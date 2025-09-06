#!/bin/bash
# Setup script for internal documentation protection
# Run this after cloning the repository to prevent accidental upstream submissions

echo "Setting up internal documentation protection..."

# Create .git/hooks directory if it doesn't exist
mkdir -p .git/hooks

# Create pre-commit hook
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash
# Pre-commit hook to prevent internal documentation from being committed to upstream branches

# List of protected files that should NEVER go upstream
PROTECTED_FILES=(
    "ARCHITECTURE.md"
    "BRANCHES.md"
    "CLAUDE.md"
    "DESIGN.md"
    "DOCKER.md"
    "ROADMAP.md"
    "STRATEGY.md"
    "TESTING.md"
    ".gitignore-internal"
)

# Get current branch
BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Check if we're on a branch that might go upstream
if [[ "$BRANCH" == "main" ]] || [[ "$BRANCH" == shadnygren/fix-* ]] || [[ "$BRANCH" == shadnygren/feature-* ]]; then
    # Check if any protected files are staged
    for file in "${PROTECTED_FILES[@]}"; do
        if git diff --cached --name-only | grep -q "^$file$"; then
            echo "❌ ERROR: Protected file '$file' cannot be committed to branch '$BRANCH'"
            echo "This file is for internal use only and must not be sent upstream to ByteDance."
            echo ""
            echo "To fix this:"
            echo "  git reset HEAD $file"
            echo ""
            exit 1
        fi
    done
    
    # Also check for any SHAD* files
    if git diff --cached --name-only | grep -q "SHAD"; then
        echo "❌ ERROR: Files with 'SHAD' in the name cannot be committed to branch '$BRANCH'"
        exit 1
    fi
fi

exit 0
EOF

# Make hook executable
chmod +x .git/hooks/pre-commit

# Create pre-push hook for extra protection
cat > .git/hooks/pre-push << 'EOF'
#!/bin/bash
# Pre-push hook to prevent internal documentation from being pushed upstream

PROTECTED_FILES=(
    "ARCHITECTURE.md"
    "BRANCHES.md"
    "CLAUDE.md"
    "DESIGN.md"
    "DOCKER.md"
    "ROADMAP.md"
    "STRATEGY.md"
    "TESTING.md"
    ".gitignore-internal"
)

# Get remote name and URL
remote="$1"
url="$2"

# Check if pushing to ByteDance upstream
if [[ "$url" == *"ByteDance"* ]] || [[ "$url" == *"bytedance"* ]]; then
    echo "⚠️  Pushing to ByteDance repository detected!"
    echo "Checking for protected files..."
    
    # Read commits being pushed
    while read local_ref local_sha remote_ref remote_sha
    do
        # Check if any protected files are in the commits
        for file in "${PROTECTED_FILES[@]}"; do
            if git diff --name-only "$remote_sha..$local_sha" | grep -q "^$file$"; then
                echo "❌ ERROR: Protected file '$file' found in commits!"
                echo "Internal documentation must not be pushed to ByteDance."
                echo ""
                echo "Protected files detected. Push aborted."
                exit 1
            fi
        done
    done
    
    echo "✅ No protected files found. Push allowed."
fi

exit 0
EOF

# Make hook executable
chmod +x .git/hooks/pre-push

echo "✅ Internal documentation protection installed!"
echo ""
echo "Protected files:"
echo "  - ARCHITECTURE.md"
echo "  - BRANCHES.md"
echo "  - CLAUDE.md"
echo "  - DESIGN.md"
echo "  - DOCKER.md"
echo "  - ROADMAP.md"
echo "  - STRATEGY.md"
echo "  - TESTING.md"
echo "  - .gitignore-internal"
echo "  - Any file with 'SHAD' in the name"
echo ""
echo "These protections will:"
echo "  1. Prevent committing protected files to main or PR branches"
echo "  2. Prevent pushing protected files to ByteDance"
echo "  3. Warn when committing to internal branches"
echo ""
echo "To bypass in emergency (NOT RECOMMENDED):"
echo "  git commit --no-verify"
echo "  git push --no-verify"