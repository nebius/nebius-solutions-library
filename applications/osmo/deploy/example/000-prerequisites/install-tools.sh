#!/bin/bash
#
# Install required tools for OSMO on Nebius deployment
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_status() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

print_error() {
    echo -e "${RED}[✗]${NC} $1"
}

# Version requirements
TERRAFORM_MIN_VERSION="1.5.0"
KUBECTL_MIN_VERSION="1.28.0"
HELM_MIN_VERSION="3.12.0"
NEBIUS_CLI_INSTALL_URL="${NEBIUS_CLI_INSTALL_URL:-https://storage.eu-north1.nebius.cloud/cli/install.sh}"

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
    else
        echo "unknown"
    fi
}

OS=$(detect_os)

# Check if command exists (including Nebius in custom locations)
check_command() {
    local cmd=$1
    if [[ "$cmd" == "nebius" ]]; then
        # Check PATH first, then common installation locations
        if command -v nebius &>/dev/null; then
            return 0
        elif [[ -x "$HOME/.nebius/bin/nebius" ]]; then
            return 0
        fi
        return 1
    else
        command -v "$cmd" &>/dev/null
    fi
}

# Get Nebius CLI path
get_nebius_path() {
    if command -v nebius &>/dev/null; then
        command -v nebius
    elif [[ -x "$HOME/.nebius/bin/nebius" ]]; then
        echo "$HOME/.nebius/bin/nebius"
    fi
}

# Compare versions (returns 0 if version >= required)
version_ge() {
    local version=$1
    local required=$2
    printf '%s\n%s' "$required" "$version" | sort -V -C
}

check_terraform() {
    if check_command terraform; then
        local version=$(terraform version -json 2>/dev/null | grep -o '"terraform_version": *"[^"]*"' | cut -d'"' -f4)
        if [[ -z "$version" ]]; then
            version=$(terraform version | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
        fi
        if version_ge "$version" "$TERRAFORM_MIN_VERSION"; then
            print_status "Terraform $version installed"
            return 0
        else
            print_warning "Terraform $version installed, but >= $TERRAFORM_MIN_VERSION recommended"
            return 0
        fi
    fi
    return 1
}

check_kubectl() {
    if check_command kubectl; then
        # Use --client flag with timeout to prevent hanging
        # Some kubectl versions try to contact server even with --client
        local version=$(timeout 5 kubectl version --client --short 2>/dev/null | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1 | tr -d 'v')
        if [[ -z "$version" ]]; then
            # Fallback: try without --short flag
            version=$(timeout 5 kubectl version --client 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
        fi
        if [[ -n "$version" ]]; then
            print_status "kubectl $version installed"
            return 0
        else
            # kubectl exists but version check failed - still report as installed
            print_status "kubectl installed (version check skipped)"
            return 0
        fi
    fi
    return 1
}

check_helm() {
    if check_command helm; then
        local version=$(helm version --short 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
        if [[ -n "$version" ]]; then
            print_status "Helm $version installed"
            return 0
        fi
    fi
    return 1
}

check_nebius() {
    if check_command nebius; then
        local nebius_path=$(get_nebius_path)
        local version=$("$nebius_path" version 2>/dev/null | head -1 || echo "unknown")
        print_status "Nebius CLI installed ($version)"
        if [[ "$nebius_path" == "$HOME/.nebius/bin/nebius" ]] && ! command -v nebius &>/dev/null; then
            print_warning "Nebius CLI not in PATH. Run this first:"
            echo ""
            echo "         export PATH=\"\$HOME/.nebius/bin:\$PATH\""
            echo ""
        fi
        return 0
    fi
    return 1
}

check_osmo() {
    if check_command osmo; then
        local version=$(osmo --version 2>/dev/null | head -1 || echo "unknown")
        print_status "OSMO CLI installed ($version)"
        return 0
    fi
    return 1
}

install_terraform() {
    echo "Installing Terraform..."
    case $OS in
        linux|wsl)
            wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
            echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
            sudo apt-get update && sudo apt-get install -y terraform
            ;;
        macos)
            brew tap hashicorp/tap
            brew install hashicorp/tap/terraform
            ;;
    esac
}

install_kubectl() {
    echo "Installing kubectl..."
    case $OS in
        linux|wsl)
            curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
            chmod +x kubectl
            sudo mv kubectl /usr/local/bin/
            ;;
        macos)
            brew install kubectl
            ;;
    esac
}

install_helm() {
    echo "Installing Helm..."
    case $OS in
        linux|wsl)
            curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
            ;;
        macos)
            brew install helm
            ;;
    esac
}

install_nebius() {
    echo "Installing Nebius CLI..."
    # Note: URL updated per https://docs.nebius.com/cli/install
    curl -sSL "$NEBIUS_CLI_INSTALL_URL" | bash
    
    # Add to PATH for current session
    export PATH="$HOME/.nebius/bin:$PATH"
    
    print_warning "Nebius CLI installed to ~/.nebius/bin/"
    print_warning "Add to your shell profile: export PATH=\"\$HOME/.nebius/bin:\$PATH\""
}

install_osmo() {
    echo "Installing OSMO CLI..."
    # Install via official NVIDIA install script
    # See: https://nvidia.github.io/OSMO/main/user_guide/getting_started/install/index.html
    curl -fsSL https://raw.githubusercontent.com/NVIDIA/OSMO/refs/heads/main/install.sh | bash
    
    # The install script typically adds osmo to ~/.local/bin or similar
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        export PATH="$HOME/.local/bin:$PATH"
    fi
    print_status "OSMO CLI installed"
}

# Main logic
main() {
    echo "========================================"
    echo "OSMO on Nebius - Tool Installer"
    echo "========================================"
    echo ""
    echo "Detected OS: $OS"
    echo ""
    
    local check_only=false
    if [[ "$1" == "--check" ]]; then
        check_only=true
        echo "Checking installed tools..."
        echo ""
    fi
    
    local all_installed=true
    
    # Check/Install Terraform
    if ! check_terraform; then
        all_installed=false
        if $check_only; then
            print_error "Terraform not installed"
        else
            install_terraform
            check_terraform || print_error "Failed to install Terraform"
        fi
    fi
    
    # Check/Install kubectl
    if ! check_kubectl; then
        all_installed=false
        if $check_only; then
            print_error "kubectl not installed"
        else
            install_kubectl
            check_kubectl || print_error "Failed to install kubectl"
        fi
    fi
    
    # Check/Install Helm
    if ! check_helm; then
        all_installed=false
        if $check_only; then
            print_error "Helm not installed"
        else
            install_helm
            check_helm || print_error "Failed to install Helm"
        fi
    fi
    
    # Check/Install Nebius CLI
    if ! check_nebius; then
        all_installed=false
        if $check_only; then
            print_error "Nebius CLI not installed"
        else
            install_nebius
            check_nebius || print_error "Failed to install Nebius CLI"
        fi
    fi
    
    # Check/Install OSMO CLI (for backend deployment and workflow management)
    if ! check_osmo; then
        all_installed=false
        if $check_only; then
            print_error "OSMO CLI not installed"
        else
            install_osmo
            check_osmo || print_error "Failed to install OSMO CLI"
        fi
    fi
    
    echo ""
    if $all_installed; then
        echo "========================================"
        print_status "All required tools are installed!"
        echo "========================================"
        echo ""
        echo "Next step: Configure your Nebius environment"
        echo "  source ./nebius-env-init.sh"
    else
        if $check_only; then
            echo "========================================"
            print_warning "Some tools are missing. Run without --check to install."
            echo "========================================"
            exit 1
        fi
    fi
}

main "$@"
