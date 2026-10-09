#!/bin/bash
#
# WireGuard Client Setup Script
#
# This script helps configure a WireGuard client to connect to the
# OSMO cluster's private network.
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo ""
echo "========================================"
echo "  WireGuard Client Setup"
echo "========================================"
echo ""

# Detect OS
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if grep -q Microsoft /proc/version 2>/dev/null; then
            echo "wsl"
        else
            echo "linux"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]]; then
        echo "windows"
    else
        echo "unknown"
    fi
}

OS=$(detect_os)
echo "Detected OS: $OS"
echo ""

# Check if WireGuard is installed
check_wireguard() {
    case $OS in
        linux|wsl)
            if command -v wg &>/dev/null; then
                return 0
            fi
            ;;
        macos)
            if [[ -d "/Applications/WireGuard.app" ]] || command -v wg &>/dev/null; then
                return 0
            fi
            ;;
        windows)
            if [[ -d "/c/Program Files/WireGuard" ]]; then
                return 0
            fi
            ;;
    esac
    return 1
}

# Install WireGuard
install_wireguard() {
    echo -e "${BLUE}Installing WireGuard...${NC}"
    case $OS in
        linux)
            sudo apt-get update
            sudo apt-get install -y wireguard wireguard-tools
            ;;
        wsl)
            echo -e "${YELLOW}For WSL, install WireGuard on Windows:${NC}"
            echo "  1. Download from: https://www.wireguard.com/install/"
            echo "  2. Install the Windows application"
            echo "  3. Import the configuration file"
            return 1
            ;;
        macos)
            echo "Install WireGuard from the App Store or:"
            echo "  brew install wireguard-tools"
            return 1
            ;;
        *)
            echo "Please install WireGuard manually from: https://www.wireguard.com/install/"
            return 1
            ;;
    esac
}

# Get Terraform outputs
get_terraform_output() {
    local output_name=$1
    cd ../001-iac 2>/dev/null || {
        echo -e "${RED}[ERROR]${NC} Cannot find ../001-iac directory"
        return 1
    }
    terraform output -raw "$output_name" 2>/dev/null
    cd - >/dev/null
}

# Generate client keys
generate_client_keys() {
    local private_key=$(wg genkey)
    local public_key=$(echo "$private_key" | wg pubkey)
    echo "$private_key|$public_key"
}

# Main setup
main() {
    # Check/Install WireGuard
    if ! check_wireguard; then
        install_wireguard || {
            echo ""
            echo -e "${RED}[ERROR]${NC} WireGuard not installed. Please install manually."
            exit 1
        }
    fi
    echo -e "${GREEN}[✓]${NC} WireGuard installed"
    echo ""
    
    # Check if WireGuard was enabled in Terraform
    echo -e "${BLUE}Retrieving WireGuard server information...${NC}"
    
    local wg_public_ip=$(get_terraform_output "wireguard.public_ip" 2>/dev/null || echo "")
    
    if [[ -z "$wg_public_ip" || "$wg_public_ip" == "null" ]]; then
        echo -e "${RED}[ERROR]${NC} WireGuard VPN was not deployed."
        echo ""
        echo "To enable WireGuard, set in terraform.tfvars:"
        echo "  enable_wireguard = true"
        echo ""
        echo "Then run: terraform apply"
        exit 1
    fi
    
    local wg_ui_url=$(get_terraform_output "wireguard.ui_url" 2>/dev/null || echo "")
    
    echo ""
    echo -e "${GREEN}[✓]${NC} WireGuard server found"
    echo "    Public IP: $wg_public_ip"
    if [[ -n "$wg_ui_url" && "$wg_ui_url" != "null" ]]; then
        echo "    Web UI:    $wg_ui_url"
    fi
    echo ""
    
    # Instructions for manual configuration
    echo "========================================"
    echo -e "${BLUE}Configuration Instructions${NC}"
    echo "========================================"
    echo ""
    echo "Option 1: Use WireGuard Web UI (Recommended)"
    echo "  1. Open in browser: $wg_ui_url"
    echo "  2. Login with the generated password (check Terraform output)"
    echo "  3. Create a new client configuration"
    echo "  4. Download the configuration file"
    echo "  5. Import into WireGuard client"
    echo ""
    echo "Option 2: Manual Configuration"
    echo "  1. Generate client keys: wg genkey | tee privatekey | wg pubkey > publickey"
    echo "  2. SSH to WireGuard server and add peer"
    echo "  3. Create local configuration file"
    echo ""
    
    # Create config template
    local config_file="wg-client-osmo.conf"
    
    if [[ "$OS" == "linux" ]] && command -v wg &>/dev/null; then
        echo -e "${BLUE}Generating client configuration template...${NC}"
        
        local keys=$(generate_client_keys)
        local client_private_key=$(echo "$keys" | cut -d'|' -f1)
        local client_public_key=$(echo "$keys" | cut -d'|' -f2)
        
        cat > "$config_file" << EOF
[Interface]
# Client private key (generated)
PrivateKey = $client_private_key
# Client IP address in VPN network (change if needed)
Address = 10.8.0.2/24
DNS = 8.8.8.8

[Peer]
# WireGuard server public key (get from server)
PublicKey = <SERVER_PUBLIC_KEY>
# Allowed IPs - route all traffic through VPN
AllowedIPs = 10.8.0.0/24, 10.0.0.0/16
# WireGuard server endpoint
Endpoint = $wg_public_ip:51820
# Keep connection alive
PersistentKeepalive = 25
EOF
        
        echo ""
        echo -e "${GREEN}[✓]${NC} Configuration template created: $config_file"
        echo ""
        echo "Your client public key (add this to server):"
        echo "  $client_public_key"
        echo ""
        echo "Next steps:"
        echo "  1. Get server public key from WireGuard Web UI or server"
        echo "  2. Add your client public key to server"
        echo "  3. Update <SERVER_PUBLIC_KEY> in $config_file"
        echo "  4. Start VPN: sudo wg-quick up ./$config_file"
    fi
    
    echo ""
    echo "========================================"
    echo -e "${GREEN}Setup guide complete!${NC}"
    echo "========================================"
}

main "$@"
