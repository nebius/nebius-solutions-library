#!/bin/bash
#
# OSMO on Nebius - Secrets Initialization Script
#
# This script generates secrets and stores them in Nebius MysteryBox.
# Secrets are NOT stored in Terraform state - only the secret IDs are used.
#
# Usage:
#   source ./secrets-init.sh
#
# Prerequisites:
#   - Nebius CLI installed and authenticated
#   - Environment variables set (run nebius-env-init.sh first)
#   - jq installed
#

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Default secret names
POSTGRESQL_SECRET_NAME="${OSMO_POSTGRESQL_SECRET_NAME:-osmo-postgresql-password}"
MEK_SECRET_NAME="${OSMO_MEK_SECRET_NAME:-osmo-mek}"

echo ""
echo "========================================"
echo "  OSMO Secrets Initialization"
echo "========================================"
echo ""

# If env not set, source nebius-env-init.sh from this script's directory (so ./secrets-init.sh works without prior source)
if [[ -z "${NEBIUS_PROJECT_ID:-}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
    if [[ -f "${SCRIPT_DIR}/nebius-env-init.sh" ]]; then
        echo "Sourcing ${SCRIPT_DIR}/nebius-env-init.sh (NEBIUS_PROJECT_ID not set)..."
        # shellcheck source=./nebius-env-init.sh
        source "${SCRIPT_DIR}/nebius-env-init.sh"
        echo ""
    fi
fi

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

# Read input with a prompt into a variable (bash/zsh compatible).
read_prompt_var() {
    local prompt=$1
    local var_name=$2
    local default=$3
    local value=""
    local read_from="/dev/tty"
    local write_to="/dev/tty"

    if [[ ! -r "/dev/tty" || ! -w "/dev/tty" ]]; then
        read_from="/dev/stdin"
        write_to="/dev/stdout"
    fi

    if [[ -n "$default" ]]; then
        printf "%s [%s]: " "$prompt" "$default" >"$write_to"
    else
        printf "%s: " "$prompt" >"$write_to"
    fi

    IFS= read -r value <"$read_from"
    if [[ -z "$value" && -n "$default" ]]; then
        value="$default"
    fi

    eval "$var_name='$value'"
}

# Return a random integer in range [min, max] using /dev/urandom.
rand_int() {
    local min=$1
    local max=$2
    local range=$((max - min + 1))
    local num=""

    while :; do
        num=$(od -An -N2 -tu2 /dev/urandom | tr -d ' ')
        if [[ -n "$num" ]]; then
            echo $((min + num % range))
            return 0
        fi
    done
}

# Pick a random character from a set.
rand_char_from_set() {
    local set=$1
    local idx
    idx=$(rand_int 0 $((${#set} - 1)))
    printf "%s" "${set:$idx:1}"
}

# Shuffle a string using Fisher-Yates.
shuffle_string() {
    local input=$1
    local -a chars
    local i j tmp
    local len=${#input}

    if [[ -n "${BASH_VERSION:-}" ]]; then
        for ((i = 0; i < len; i++)); do
            chars[i]="${input:i:1}"
        done
        for ((i = len - 1; i > 0; i--)); do
            j=$(rand_int 0 "$i")
            tmp="${chars[i]}"
            chars[i]="${chars[j]}"
            chars[j]="$tmp"
        done
        local out=""
        for ((i = 0; i < len; i++)); do
            out+="${chars[i]}"
        done
        printf "%s" "$out"
    else
        # zsh uses 1-based indexing for arrays and string subscripts
        for ((i = 1; i <= len; i++)); do
            chars[i]="${input[$i]}"
        done
        for ((i = len; i > 1; i--)); do
            j=$(rand_int 1 "$i")
            tmp="${chars[i]}"
            chars[i]="${chars[j]}"
            chars[j]="$tmp"
        done
        local out=""
        for ((i = 1; i <= len; i++)); do
            out+="${chars[i]}"
        done
        printf "%s" "$out"
    fi
}

get_nebius_path() {
    if command -v nebius &>/dev/null; then
        command -v nebius
    elif [[ -x "$HOME/.nebius/bin/nebius" ]]; then
        echo "$HOME/.nebius/bin/nebius"
    fi
}

check_prerequisites() {
    echo -e "${BLUE}Step 1: Checking prerequisites${NC}"
    
    # Check Nebius CLI
    local nebius_path=$(get_nebius_path)
    if [[ -z "$nebius_path" ]]; then
        echo -e "${RED}[ERROR]${NC} Nebius CLI not found"
        echo "  Run: ./install-tools.sh"
        return 1
    fi
    echo -e "${GREEN}[✓]${NC} Nebius CLI found"
    
    # Check jq
    if ! command -v jq &>/dev/null; then
        echo -e "${RED}[ERROR]${NC} jq not found"
        echo "  Install: sudo apt-get install jq"
        return 1
    fi
    echo -e "${GREEN}[✓]${NC} jq found"
    
    # Check openssl
    if ! command -v openssl &>/dev/null; then
        echo -e "${RED}[ERROR]${NC} openssl not found"
        return 1
    fi
    echo -e "${GREEN}[✓]${NC} openssl found"
    
    # Check environment variables
    if [[ -z "$NEBIUS_PROJECT_ID" ]]; then
        echo -e "${RED}[ERROR]${NC} NEBIUS_PROJECT_ID not set"
        echo "  Run: source ./nebius-env-init.sh"
        return 1
    fi
    echo -e "${GREEN}[✓]${NC} NEBIUS_PROJECT_ID set: $NEBIUS_PROJECT_ID"
    
    echo ""
    return 0
}

# Generate secure password meeting Nebius PostgreSQL requirements
generate_postgresql_password() {
    # Requirements:
    # - Min 8 characters (we use 32)
    # - At least one lowercase, uppercase, digit, special char
    # - No % character
    
    local password=""
    local attempts=0
    local max_attempts=10
    
    while [[ $attempts -lt $max_attempts ]]; do
        # Generate base password
        password=$(openssl rand -base64 32 | tr -d '/+=\n' | head -c 28)
        
        # Add required character types
        local lower=$(rand_char_from_set "abcdefghijklmnopqrstuvwxyz")
        local upper=$(rand_char_from_set "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        local digit=$(rand_char_from_set "0123456789")
        local special=$(rand_char_from_set '!#$^&*()-_=+')
        
        password="${password}${lower}${upper}${digit}${special}"
        
        # Shuffle the password
        password=$(shuffle_string "$password")
        
        # Verify requirements
        if [[ ${#password} -ge 32 ]] && \
           [[ "$password" =~ [a-z] ]] && \
           [[ "$password" =~ [A-Z] ]] && \
           [[ "$password" =~ [0-9] ]] && \
           [[ "$password" =~ [\!\#\$\^\&\*\(\)\-\_\=\+] ]] && \
           [[ ! "$password" =~ [%@:\/\;\[\]\{\}\|\<\>\,\.\?] ]]; then
            echo "$password"
            return 0
        fi
        
        ((attempts++))
    done
    
    echo -e "${RED}[ERROR]${NC} Failed to generate valid password after $max_attempts attempts"
    return 1
}

# Generate MEK (Master Encryption Key) for OSMO
generate_mek() {
    # MEK is a JWK (JSON Web Key) format
    # OSMO expects: {"currentMek": "key1", "meks": {"key1": "<base64-jwk>"}}
    
    # Generate a 256-bit key
    local key_bytes=$(openssl rand 32)
    local key_base64=$(echo -n "$key_bytes" | base64 | tr -d '\n')
    
    # Create JWK structure (symmetric key)
    local jwk=$(cat <<EOF
{
  "kty": "oct",
  "k": "$key_base64",
  "alg": "A256GCM",
  "use": "enc"
}
EOF
)
    
    # Encode the JWK as base64 for storage
    local jwk_base64=$(echo -n "$jwk" | base64 | tr -d '\n')
    
    # Return the full MEK structure
    cat <<EOF
{"currentMek":"key1","meks":{"key1":"$jwk_base64"}}
EOF
}

# Check if secret exists in MysteryBox
secret_exists() {
    local parent_id=$1
    local secret_name=$2
    local nebius_path=$(get_nebius_path)
    
    local result=$("$nebius_path" mysterybox v1 secret get-by-name \
        --parent-id "$parent_id" \
        --name "$secret_name" \
        --format json 2>/dev/null)
    
    # Extract JSON from output (CLI may print info messages before JSON)
    local json_result=$(echo "$result" | awk '/^{/,0')
    
    if [[ -n "$json_result" && "$json_result" != "null" ]]; then
        echo "$json_result" | jq -r '.metadata.id'
        return 0
    fi
    return 1
}

# Create secret in MysteryBox
create_secret() {
    local parent_id=$1
    local secret_name=$2
    local key=$3
    local value=$4
    local nebius_path=$(get_nebius_path)
    
    # Escape special characters in value for JSON
    local escaped_value=$(echo -n "$value" | jq -Rs '.')
    # Remove surrounding quotes added by jq
    escaped_value=${escaped_value:1:-1}
    
    local payload="[{\"key\":\"$key\",\"string_value\":\"$escaped_value\"}]"
    
    local result=$("$nebius_path" mysterybox v1 secret create \
        --parent-id "$parent_id" \
        --name "$secret_name" \
        --secret-version-payload "$payload" \
        --format json 2>&1)
    
    local exit_code=$?
    
    # Extract JSON from output (CLI may print info messages before JSON)
    # Find the first line starting with '{' and print everything from there
    local json_result=$(echo "$result" | awk '/^{/,0')
    
    if [[ $exit_code -eq 0 && -n "$json_result" ]]; then
        echo "$json_result" | jq -r '.metadata.id'
        return 0
    else
        echo -e "${RED}[ERROR]${NC} Failed to create secret: $result"
        return 1
    fi
}

# Delete secret from MysteryBox
delete_secret() {
    local secret_id=$1
    local nebius_path=$(get_nebius_path)
    
    "$nebius_path" mysterybox v1 secret delete --id "$secret_id" 2>/dev/null
}

# -----------------------------------------------------------------------------
# Main Secret Creation Functions
# -----------------------------------------------------------------------------

create_postgresql_secret() {
    echo -e "${BLUE}Creating PostgreSQL password secret...${NC}"
    
    # Check if secret already exists
    local existing_id=$(secret_exists "$NEBIUS_PROJECT_ID" "$POSTGRESQL_SECRET_NAME")
    
    if [[ -n "$existing_id" ]]; then
        echo -e "${YELLOW}[!]${NC} Secret '$POSTGRESQL_SECRET_NAME' already exists (ID: $existing_id)"
        read_prompt_var "  Replace existing secret? (y/N)" replace ""
        if [[ "$replace" =~ ^[Yy]$ ]]; then
            echo "  Deleting existing secret..."
            delete_secret "$existing_id"
            sleep 2
        else
            echo "  Using existing secret"
            export OSMO_POSTGRESQL_SECRET_ID="$existing_id"
            export TF_VAR_postgresql_mysterybox_secret_id="$existing_id"
            return 0
        fi
    fi
    
    # Generate password
    echo "  Generating secure password..."
    local password=$(generate_postgresql_password)
    if [[ $? -ne 0 || -z "$password" ]]; then
        echo -e "${RED}[ERROR]${NC} Failed to generate password"
        return 1
    fi
    echo -e "${GREEN}[✓]${NC} Password generated (length: ${#password})"
    
    # Store in MysteryBox
    echo "  Storing in MysteryBox..."
    local secret_id=$(create_secret "$NEBIUS_PROJECT_ID" "$POSTGRESQL_SECRET_NAME" "password" "$password")
    
    if [[ $? -eq 0 && -n "$secret_id" ]]; then
        echo -e "${GREEN}[✓]${NC} PostgreSQL secret created: $secret_id"
        export OSMO_POSTGRESQL_SECRET_ID="$secret_id"
        export TF_VAR_postgresql_mysterybox_secret_id="$secret_id"
        return 0
    else
        echo -e "${RED}[ERROR]${NC} Failed to create PostgreSQL secret"
        return 1
    fi
}

create_mek_secret() {
    echo -e "${BLUE}Creating MEK (Master Encryption Key) secret...${NC}"
    
    # Check if secret already exists
    local existing_id=$(secret_exists "$NEBIUS_PROJECT_ID" "$MEK_SECRET_NAME")
    
    if [[ -n "$existing_id" ]]; then
        echo -e "${YELLOW}[!]${NC} Secret '$MEK_SECRET_NAME' already exists (ID: $existing_id)"
        read_prompt_var "  Replace existing secret? (y/N)" replace ""
        if [[ "$replace" =~ ^[Yy]$ ]]; then
            echo "  Deleting existing secret..."
            delete_secret "$existing_id"
            sleep 2
        else
            echo "  Using existing secret"
            export OSMO_MEK_SECRET_ID="$existing_id"
            export TF_VAR_mek_mysterybox_secret_id="$existing_id"
            return 0
        fi
    fi
    
    # Generate MEK
    echo "  Generating Master Encryption Key..."
    local mek=$(generate_mek)
    if [[ $? -ne 0 || -z "$mek" ]]; then
        echo -e "${RED}[ERROR]${NC} Failed to generate MEK"
        return 1
    fi
    echo -e "${GREEN}[✓]${NC} MEK generated"
    
    # Store in MysteryBox
    echo "  Storing in MysteryBox..."
    local secret_id=$(create_secret "$NEBIUS_PROJECT_ID" "$MEK_SECRET_NAME" "mek" "$mek")
    
    if [[ $? -eq 0 && -n "$secret_id" ]]; then
        echo -e "${GREEN}[✓]${NC} MEK secret created: $secret_id"
        export OSMO_MEK_SECRET_ID="$secret_id"
        export TF_VAR_mek_mysterybox_secret_id="$secret_id"
        return 0
    else
        echo -e "${RED}[ERROR]${NC} Failed to create MEK secret"
        return 1
    fi
}


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

main() {
    # Check prerequisites
    if ! check_prerequisites; then
        return 1
    fi
    
    echo -e "${BLUE}Step 2: Creating secrets in MysteryBox${NC}"
    echo ""
    
    local success=true
    
    # Create PostgreSQL secret
    if ! create_postgresql_secret; then
        success=false
    fi
    echo ""
    
    # Create MEK secret
    if ! create_mek_secret; then
        success=false
    fi
    echo ""
    
    if ! $success; then
        echo -e "${RED}[ERROR]${NC} Some secrets failed to create"
        return 1
    fi
    
    # Summary
    echo "========================================"
    echo -e "${GREEN}Secrets initialization complete!${NC}"
    echo "========================================"
    echo ""
    echo "Environment variables exported:"
    echo "  TF_VAR_postgresql_mysterybox_secret_id = $TF_VAR_postgresql_mysterybox_secret_id"
    echo "  TF_VAR_mek_mysterybox_secret_id        = $TF_VAR_mek_mysterybox_secret_id"
    echo ""
    echo "Secrets are stored in MysteryBox. Run this script again in a new"
    echo "terminal session to retrieve existing secrets by name."
    echo ""
    echo "To retrieve secret values manually:"
    echo "  # PostgreSQL password:"
    echo "  nebius mysterybox v1 payload get-by-key --secret-id $TF_VAR_postgresql_mysterybox_secret_id --key password --format json | jq -r '.data.string_value'"
    echo ""
    echo "  # MEK:"
    echo "  nebius mysterybox v1 payload get-by-key --secret-id $TF_VAR_mek_mysterybox_secret_id --key mek --format json | jq -r '.data.string_value'"
    echo ""
    echo "Next steps:"
    echo "  1. cd ../001-iac"
    echo "  2. cp terraform.tfvars.cost-optimized.example terraform.tfvars  # or another preset"
    echo "  3. terraform init && terraform apply"
    echo ""
    
    return 0
}

# Run main
main
