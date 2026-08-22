"""Import d'une stack depuis un répertoire externe (extrait de docker_manager).

Copie ``docker-compose.yml`` (+ ``.env`` et fichiers de config) vers
``data/stacks/{name}``, convertit les volumes relatifs en chemins absolus et
supporte le mode ``dry_run``. ``import_stack`` est ré-exporté dans le namespace
``agent.docker_manager``. ``_git_init`` / ``_git_save`` (module git_history)
sont monkeypatchés par les tests via ``agent.docker_manager`` → ils sont résolus
ici via ``_dm()``.
"""

import shutil
from datetime import date
from pathlib import Path

from agent.config import get_data_dir
from agent.docker.validation import validate_stack_name


def _dm():
    """Résolution tardive du namespace agent.docker_manager (évite tout cycle)."""
    from agent import docker_manager
    return docker_manager


def import_stack(source_path: str, stack_name: str = None, dry_run: bool = False) -> dict:
    """Import a stack from an external directory (e.g. Dockge).
    Copies docker-compose.yml + .env to /data/stacks/{name}/
    Converts relative volume paths to absolute paths.

    When *dry_run* is True, no file is written or copied: the function only
    performs path conversion and returns a preview of the converted compose
    file along with the list of conversions and warnings.

    Returns: { success: bool, name: str, conversions: list, warnings: list,
               preview?: str }
    """
    import re as _re
    from datetime import date

    source = Path(source_path).resolve()
    if not source.exists():
        return {"success": False, "error": f"Source path '{source_path}' does not exist"}

    compose_src = source / 'docker-compose.yml'
    if not compose_src.exists():
        # Try compose.yaml
        compose_src = source / 'compose.yaml'
        if not compose_src.exists():
            return {"success": False, "error": "No docker-compose.yml found in source directory"}

    # Determine stack name
    if not stack_name:
        stack_name = source.name

    # Validate stack name
    try:
        validate_stack_name(stack_name)
    except ValueError:
        return {"success": False, "error": f"Invalid stack name: {stack_name}"}

    # Target directory
    stacks_dir = Path(get_data_dir()) / 'stacks'
    target = stacks_dir / stack_name

    # In dry-run mode, do not check for an existing target folder: the user
    # might want to preview even if a folder with the same name already
    # exists (the real import will fail then).
    if not dry_run and target.exists():
        return {"success": False, "error": f"Stack '{stack_name}' already exists in Docky"}

    # Read the compose file
    compose_content = compose_src.read_text(encoding='utf-8')

    # Convert relative paths to absolute
    conversions = []
    warnings = []

    lines = compose_content.split('\n')
    converted_lines = []

    # Get named volumes to avoid converting them
    named_volumes = set()
    in_volumes_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == 'volumes:' and not line.startswith(' '):
            in_volumes_section = True
            continue
        if in_volumes_section:
            if line and not line.startswith(' ') and not line.startswith('#'):
                in_volumes_section = False
            elif stripped and not stripped.startswith('-'):
                # Named volume: volumename:  (a YAML key ending with ':')
                key_part = stripped.split('#')[0].strip()
                if key_part.endswith(':'):
                    vol_name = key_part.rstrip(':').strip()
                    if vol_name:
                        named_volumes.add(vol_name)

    # Track the current YAML path to detect which section we're in
    indent_levels = [0]  # stack of indent levels
    yaml_path: list[str] = []  # e.g. ["services", "n8n", "volumes"]

    for line in lines:
        stripped = line.strip()

        # Skip comments and empty lines
        if stripped.startswith('#') or not stripped:
            converted_lines.append(line)
            continue

        indent = len(line) - len(line.lstrip())

        # --- Update YAML path based on indentation ---
        # If we encounter a YAML key (line ending with ':' not starting with '-'),
        # update the indent stack and yaml_path accordingly.
        if stripped.endswith(':') and not stripped.startswith('- '):
            key = stripped.rstrip(':').strip().split('#')[0].strip()
            if key:
                # Pop indent levels deeper than current line
                while len(indent_levels) > 1 and indent_levels[-1] > indent:
                    indent_levels.pop()
                    if yaml_path:
                        yaml_path.pop()

                if indent > indent_levels[-1]:
                    # Entering a new nested level -> push
                    indent_levels.append(indent)
                    yaml_path.append(key)
                elif indent == indent_levels[-1]:
                    # Same level as previous key -> replace
                    if yaml_path:
                        yaml_path[-1] = key
                    else:
                        yaml_path.append(key)
                # If indent < indent_levels[-1], the while loop above already
                # popped all deeper levels, and we are now at a shallower level
                # which means it's a totally different branch (e.g. top-level
                # volumes: after services:). In that case, replace the last key
                # if we're at the same indent as the remaining stack top.
                elif indent == (indent_levels[-1] if indent_levels else 0):
                    if yaml_path:
                        yaml_path[-1] = key
                    else:
                        yaml_path.append(key)

        # Determine if we are inside a service-level "volumes:" section
        # (not top-level volumes which are named volume declarations).
        is_in_volumes = (
            'volumes' in yaml_path
            and yaml_path.index('volumes') > 0
        )

        # Look for volume mounts: - source:target or - source:target:ro
        if stripped.startswith('- '):
            if not is_in_volumes:
                # Not in a volumes section - pass through unchanged
                converted_lines.append(line)
                continue

            vol_part = stripped[2:].strip()
            # Split by : but be careful of Windows paths (not relevant here)
            parts = vol_part.split(':')
            if len(parts) >= 2:
                source_vol = parts[0].strip()

                # Skip if it's a named volume
                if source_vol in named_volumes:
                    converted_lines.append(line)
                    continue

                # Skip if it's already absolute path
                if source_vol.startswith('/'):
                    converted_lines.append(line)
                    continue

                # Skip if it looks like a variable ${...}
                if '${' in source_vol:
                    warnings.append(f"Variable in volume path: {source_vol} - check manually")
                    converted_lines.append(line)
                    continue

                # Skip if it's a relative path that goes up (../)
                if source_vol.startswith('../'):
                    warnings.append(f"Parent directory path: {source_vol} - check manually")
                    converted_lines.append(line)
                    continue

                # Convert: ./something or something → /abs/path/something
                if source_vol.startswith('./'):
                    source_vol = source_vol[2:]

                # Resolve relative to source directory
                abs_path = str((source / source_vol).resolve())

                # Replace in the line
                new_line = line.replace(source_vol if not parts[0].strip().startswith('./') else './' + source_vol, abs_path)
                if new_line == line:
                    # Fallback: replace the whole source part
                    indent_str = line[:len(line) - len(line.lstrip())]
                    rest = ':'.join(parts[1:])
                    new_line = f"{indent_str}- {abs_path}:{rest}"

                conversions.append(f"{parts[0].strip()} → {abs_path}")
                converted_lines.append(new_line)
                continue

        converted_lines.append(line)

    converted_compose = '\n'.join(converted_lines)

    # Add Docky metadata at the top
    today = date.today().isoformat()
    metadata = f"""# ============================================
# Docky Stack Metadata
# @name: {stack_name}
# @category: imported
# @description: Imported from {source}
# @source: 
# @hardware: 
# @ports: 
# @created: {today}
# @updated: {today}
# ============================================

"""

    # In dry-run mode, do not write or copy anything: just return the
    # preview of the converted compose file.
    if dry_run:
        return {
            "success": True,
            "name": stack_name,
            "conversions": conversions,
            "warnings": warnings,
            "preview": metadata + converted_compose,
        }

    # Create target directory
    target.mkdir(parents=True, exist_ok=False)

    # Write the compose file with metadata
    (target / 'docker-compose.yml').write_text(metadata + converted_compose, encoding='utf-8')

    # Copy .env if exists
    env_src = source / '.env'
    if env_src.exists():
        shutil.copy2(str(env_src), str(target / '.env'))

    # Copy other config files (not docker-compose.yml, not .env, not .git)
    for item in source.iterdir():
        if item.is_file() and item.name not in ['docker-compose.yml', 'compose.yaml', '.env', '.gitignore', '.git']:
            # Only copy config files (yml, yaml, conf, json, txt, sh, env)
            if item.suffix in ['.yml', '.yaml', '.conf', '.json', '.txt', '.sh', '.env', '.ini', '.cfg']:
                shutil.copy2(str(item), str(target / item.name))

    # Commit the import into git history
    _dm()._git_init()
    _dm()._git_save(stack_name, f"Import de {stack_name}")

    return {
        "success": True,
        "name": stack_name,
        "path": str(target),
        "conversions": conversions,
        "warnings": warnings
    }
