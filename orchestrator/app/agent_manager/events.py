"""Event-driven refresh (WebSocket events from agents).

Extracted from ``app.agent_manager.client``. Every function is written as a
method (``self`` first) and assigned back onto ``AgentManager`` in the façade,
so behaviour is unchanged.

Circular-dependency resolution
------------------------------
``_handle_agent_event`` previously imported ``app.routes.api`` lazily to
broadcast a ``docky_event`` to the connected frontends, while
``app.routes.api`` imports ``app.agent_manager.client`` at module level.

To remove this latent cycle cleanly, ``AgentManager`` now exposes a
``broadcast_agent_event`` attribute (initialised to ``None`` in
``AgentManager.__init__``). ``app.routes.api`` injects an async callback into
the singleton at import time (after defining ``_events_clients``); this module
simply awaits that callback when present. No import from ``app.routes`` remains
in the agent manager package.
"""

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


async def start_background_refresh(self):
    """Event-driven refresh: full startup + WS events + sanity check."""
    logger.info("Starting event-driven refresh (no more 5s polling)")

    # 1. Full initial refresh
    await self.refresh_all_caches()

    # 2. Connect to each agent's event stream
    for name in self.agents:
        task = asyncio.create_task(self._connect_agent_events(name))
        self._ws_tasks[name] = task

    # 3. Sanity check every 10min
    async def _sanity_loop():
        while True:
            await asyncio.sleep(600)
            for name in list(self.agents.keys()):
                if self.agents.get(name, {}).get("status") == "online":
                    try:
                        await self._incremental_refresh(name)
                    except Exception:
                        pass
    asyncio.create_task(_sanity_loop())

    # 4. Filet de sécurité: refresh périodique toutes les 60s
    async def _periodic_refresh():
        while True:
            await asyncio.sleep(60)
            try:
                await self.refresh_all_caches()
            except Exception as e:
                logger.warning("Periodic refresh failed: %s", e)

    asyncio.create_task(_periodic_refresh())


async def _connect_agent_events(self, agent_name: str):
    """Connect WebSocket to agent's /agent/events with auto-reconnect."""
    while True:
        try:
            agent = self.agents.get(agent_name)
            if not agent:
                await asyncio.sleep(10)
                continue

            agent_url = agent.get("url", "").rstrip("/")
            api_key = agent.get("api_key", "")

            # Convert http → ws, https → wss
            ws_url = agent_url.replace("http://", "ws://").replace("https://", "wss://")
            ws_url += "/agent/events"

            # The API key travels in the ``Authorization: Bearer`` header
            # (via the centralised helper), never in the URL query string, so
            # it cannot leak into proxy / access logs.
            import websockets as ws_lib
            async with ws_lib.connect(ws_url, **self._agent_ws_connect_kwargs(agent)) as ws:
                logger.info("Connected to agent '%s' events", agent_name)
                if agent_name in self.agents:
                    self.agents[agent_name]["status"] = "online"

                async for event in ws:
                    await self._handle_agent_event(agent_name, event)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("Agent '%s' events disconnected: %s", agent_name, exc)
            if agent_name in self.agents:
                self.agents[agent_name]["status"] = "offline"
            await asyncio.sleep(5)


async def _handle_agent_event(self, agent_name: str, raw):
    """Process a single Docker event from an agent."""
    if isinstance(raw, (bytes, str)):
        try:
            raw = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            return

    if not isinstance(raw, dict):
        return

    action = raw.get("Action", "")
    event_type = raw.get("Type", "")

    relevant_actions = {"start", "stop", "die", "kill", "pause", "unpause",
                        "restart", "create", "destroy", "rename"}

    if action in relevant_actions and event_type == "container":
        # Debounce: cancel previous timer for this agent
        if agent_name in self._event_debounce_timers:
            self._event_debounce_timers[agent_name].cancel()

        current_task: asyncio.Task | None = None

        async def _debounced():
            try:
                await asyncio.sleep(2)
                await self._incremental_refresh(agent_name)
            finally:
                # Only remove the entry if it still belongs to this task.
                # A new event may have already replaced it with a fresh task.
                if self._event_debounce_timers.get(agent_name) is current_task:
                    self._event_debounce_timers.pop(agent_name, None)

        current_task = asyncio.create_task(_debounced())
        self._event_debounce_timers[agent_name] = current_task

        # Broadcast aux frontends via the callback injected by app.routes.api
        # (avoids the latent ``app.agent_manager.client`` ↔ ``app.routes.api``
        # import cycle).
        broadcast = getattr(self, "broadcast_agent_event", None)
        if callable(broadcast):
            try:
                await broadcast(agent_name, action)
            except Exception:
                pass


async def _incremental_refresh(self, agent_name: str):
    """Refresh a single agent's data and rebuild aggregate caches."""
    try:
        await self.refresh_cache(agent_name)
        await self._rebuild_aggregate_cache()
    except Exception as e:
        logger.warning("Incremental refresh failed for '%s': %s", agent_name, e)
