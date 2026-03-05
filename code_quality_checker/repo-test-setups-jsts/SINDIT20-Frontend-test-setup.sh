#!/usr/bin/env bash
# Test setup for SINDIT20-Frontend (SvelteKit + TypeScript)
# Installs dependencies, generates .svelte-kit/tsconfig.json, runs Vitest.
set -euo pipefail

REPO_DIR="${1:-.}"
cd "$REPO_DIR"

echo "== SINDIT20-Frontend test setup =="

# Install deps
npm install

# Generate SvelteKit type stubs (needed for $lib/$apis resolution and tests)
npx svelte-kit sync

# Run tests via Vitest (configured in vite.config.ts / vitest-setup.js)
npm test -- --reporter=default --reporter=junit --outputFile=test-results.xml || true

echo "== SINDIT20-Frontend test setup complete =="
