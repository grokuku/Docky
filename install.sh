#!/bin/bash
set -e

# Couleurs
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo -e "${BLUE}🐳 Docky - Installation pour CachyOS${NC}"
echo "================================"
echo ""

# 1. Vérifier si on est root ou avoir sudo
if [ "$EUID" -ne 0 ]; then
    if ! sudo -v 2>/dev/null; then
        echo -e "${RED}❌ Ce script nécessite sudo pour installer Docker.${NC}"
        exit 1
    fi
fi

# 2. Installer Docker si pas présent
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}📦 Installation de Docker...${NC}"
    sudo pacman -Syu --noconfirm
    sudo pacman -S --noconfirm docker docker-compose
    echo -e "${GREEN}✅ Docker installé${NC}"
else
    echo -e "${GREEN}✅ Docker déjà installé${NC}"
fi

# 3. Activer et démarrer Docker
echo -e "${YELLOW}🔧 Activation du service Docker...${NC}"
sudo systemctl enable --now docker
echo -e "${GREEN}✅ Service Docker activé${NC}"

# 4. Ajouter l'utilisateur au groupe docker (si pas déjà)
CURRENT_USER=$(whoami)
if ! groups $CURRENT_USER | grep -q docker; then
    echo -e "${YELLOW}👥 Ajout de $CURRENT_USER au groupe docker...${NC}"
    sudo usermod -aG docker $CURRENT_USER
    echo -e "${YELLOW}⚠️  Tu devras te déconnecter/reconnecter pour que le groupe docker soit actif.${NC}"
    echo -e "${YELLOW}   Ou exécute: 'newgrp docker' dans ton terminal${NC}"
fi

# 5. Générer les clés API
echo -e "${YELLOW}🔑 Génération des clés API...${NC}"
AGENT_API_KEY=$(openssl rand -hex 32)
echo -e "${GREEN}✅ Clé API agent générée${NC}"

# 6. Configurer settings.yaml
echo -e "${YELLOW}⚙️  Configuration de settings.yaml...${NC}"
# Met à jour settings.yaml avec la clé de l'agent
# La section agents doit pointer vers http://docky-agent:8080 avec la clé générée
# Supprime l'ancienne section agents si elle existe
sed -i '/^agents:/,$d' data/settings.yaml
# Ajoute la nouvelle section agents
cat >> data/settings.yaml << EOF
agents:
  - name: "Serveur Local"
    url: "http://docky-agent:8080"
    api_key: "${AGENT_API_KEY}"
EOF
echo -e "${GREEN}✅ settings.yaml configuré${NC}"

# 7. Configurer agent/docker-compose.yml avec la clé
echo -e "${YELLOW}⚙️  Configuration de l'agent...${NC}"
# Remplace la clé API dans agent/docker-compose.yml
sed -i "s|DOCKY_AGENT_API_KEY=.*|DOCKY_AGENT_API_KEY=${AGENT_API_KEY}|" agent/docker-compose.yml
echo -e "${GREEN}✅ agent/docker-compose.yml configuré${NC}"

# 8. Créer le réseau Docker partagé
echo -e "${YELLOW}🌐 Création du réseau Docker 'docky-network'...${NC}"
docker network create docky-network 2>/dev/null || true
echo -e "${GREEN}✅ Réseau créé${NC}"

# 9. Build et démarrer l'agent
echo -e "${YELLOW}🔨 Build de l'agent...${NC}"
cd agent
docker compose down 2>/dev/null || true
docker compose build
echo -e "${YELLOW}🚀 Démarrage de l'agent...${NC}"
docker compose up -d
cd ..

# 10. Build et démarrer l'orchestrateur
echo -e "${YELLOW}🔨 Build de l'orchestrateur...${NC}"
cd orchestrator
docker compose down 2>/dev/null || true
docker compose build
echo -e "${YELLOW}🚀 Démarrage de l'orchestrateur...${NC}"
docker compose up -d
cd ..

# 11. Connecter les deux containers au réseau partagé
echo -e "${YELLOW}🌐 Connexion au réseau partagé...${NC}"
docker network connect docky-network docky-agent 2>/dev/null || true
docker network connect docky-network docky 2>/dev/null || true

# 12. Attendre que les services démarrent
echo -e "${YELLOW}⏳ Attente du démarrage des services...${NC}"
sleep 3

# 13. Vérifier que tout fonctionne
echo ""
echo -e "${BLUE}📊 Vérification...${NC}"
if docker ps | grep -q docky-agent; then
    echo -e "${GREEN}✅ Agent: running${NC}"
else
    echo -e "${RED}❌ Agent: not running (check: docker logs docky-agent)${NC}"
fi

if docker ps | grep -q docky; then
    echo -e "${GREEN}✅ Orchestrateur: running${NC}"
else
    echo -e "${RED}❌ Orchestrateur: not running (check: docker logs docky)${NC}"
fi

# 14. Afficher les infos
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}🎉 Docky est installé !${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "🌐 Interface web: ${YELLOW}http://localhost:8000${NC}"
echo -e "🔑 Login: ${YELLOW}admin${NC}"
echo -e "🔑 Mot de passe: ${YELLOW}docky123${NC}"
echo ""
echo -e "📡 Agent API: ${YELLOW}http://localhost:8080${NC}"
echo -e "🔑 Agent API Key: ${YELLOW}${AGENT_API_KEY}${NC}"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${YELLOW}⚠️  IMPORTANT:${NC}"
echo "   - Change le mot de passe par défaut (docky123)"
echo "   - Le mot de passe est dans data/users.yaml"
echo "   - Pour générer un nouveau hash:"
echo "     docker exec docky python3 -c \"import bcrypt; print(bcrypt.hashpw(b'newpassword', bcrypt.gensalt()).decode())\""
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${YELLOW}Commandes utiles:${NC}"
echo "   docker logs docky        # Logs orchestrateur"
echo "   docker logs docky-agent  # Logs agent"
echo "   cd orchestrator && docker compose down      # Arrêter Docky"
echo "   cd orchestrator && docker compose up -d     # Redémarrer Docky"
echo ""