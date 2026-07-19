#!/bin/bash
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo -e "${BLUE}🐳 Docky - Mise à jour${NC}"
echo "================================"
echo ""

# 1. Vérifier que c'est un repo git
if [ ! -d ".git" ]; then
    echo -e "${RED}❌ Ce dossier n'est pas un repo Git.${NC}"
    exit 1
fi

# 2. Sauvegarder les modifs locales
echo -e "${YELLOW}📦 Sauvegarde des modifications locales...${NC}"
STASHED=$(git stash list | wc -l)
git stash
NEW_STASHED=$(git stash list | wc -l)

if [ "$NEW_STASHED" -gt "$STASHED" ]; then
    echo -e "${GREEN}✅ Modifications sauvegardées (stash)${NC}"
    STASHED_SOMETHING=true
else
    echo -e "${GREEN}✅ Aucune modification locale à sauvegarder${NC}"
    STASHED_SOMETHING=false
fi

# 3. Pull
echo -e "${YELLOW}📥 Récupération du nouveau code...${NC}"
git pull
echo -e "${GREEN}✅ Code mis à jour${NC}"

# 4. Restaurer les modifs
if [ "$STASHED_SOMETHING" = true ]; then
    echo -e "${YELLOW}📦 Restauration des modifications locales...${NC}"
    if git stash pop; then
        echo -e "${GREEN}✅ Modifications restaurées${NC}"
    else
        echo -e "${RED}⚠️  Conflit détecté lors de la restauration !${NC}"
        echo -e "${YELLOW}   Résous les conflits manuellement avec:${NC}"
        echo "   git status"
        echo "   git diff"
        echo "   # édite les fichiers en conflit"
        echo "   git add ."
        echo "   git stash drop"
        echo ""
        echo -e "${RED}   Relance ensuite ce script après résolution.${NC}"
        exit 1
    fi
fi

# 4.5. S'assurer que le réseau Docker partagé existe
docker network create docky-network 2>/dev/null || true

# 5. Rebuild agent
echo ""
echo -e "${YELLOW}🔨 Rebuild de l'agent...${NC}"
cd agent
docker compose down 2>/dev/null || true
docker compose up -d --build
cd ..

# 6. Rebuild orchestrateur
echo -e "${YELLOW}🔨 Rebuild de l'orchestrateur...${NC}"
cd orchestrator
docker compose down 2>/dev/null || true
docker compose up -d --build
cd ..

# 7. Attendre le démarrage
echo -e "${YELLOW}⏳ Démarrage des services...${NC}"
sleep 3

# 8. Vérification
echo ""
echo -e "${BLUE}📊 Vérification...${NC}"
if docker ps | grep -q docky-agent; then
    echo -e "${GREEN}✅ Agent: running${NC}"
else
    echo -e "${RED}❌ Agent: not running (docker logs docky-agent)${NC}"
fi

if docker ps | grep -q docky; then
    echo -e "${GREEN}✅ Orchestrateur: running${NC}"
else
    echo -e "${RED}❌ Orchestrateur: not running (docker logs docky)${NC}"
fi

# 9. Résumé
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}🎉 Docky mis à jour !${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "🌐 Interface: ${YELLOW}http://localhost:8000${NC}"
echo ""
echo -e "${YELLOW}En cas de problème:${NC}"
echo "   docker logs docky        # Logs orchestrateur"
echo "   docker logs docky-agent  # Logs agent"
echo ""