#!/bin/bash
cd /home/kavia/workspace/code-generation/thought-chain-platform-214367-214408/thought_backend
source venv/bin/activate
flake8 .
LINT_EXIT_CODE=$?
if [ $LINT_EXIT_CODE -ne 0 ]; then
  exit 1
fi

