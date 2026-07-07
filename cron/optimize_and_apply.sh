#!/bin/bash
#
# optimize_and_apply.sh — Weekly param optimization pipeline (v2, auto-apply)
#
# Runs: Monday 07:00 MYT (US market CLOSED — the sweep temporarily injects
#       grid params into live db-state, so never run this during market hours)
# Chain: params_save → optimize (cross-window sandbox sweep) → auto-apply →
#        verify → report
#
# Owner directive 2026-07-07: the validated winner is applied DIRECTLY via
# runtime_config.set_param (param_history recorded, Telegram notified) — no
# approval step. The sweep itself always restores pre-sweep params first;
# only the explicit winner application changes live values.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv/bin/python3"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── 0. Save current params for potential manual rollback ──
log "Step 0: Saving current params"
$VENV -c "
import json, sys
sys.path.insert(0, '$ROOT')
from src import runtime_config
saved = {k: runtime_config.current(k)
         for k in ['entry_threshold', 'tp_atr_mult', 'sl_atr_mult']}
with open('$ROOT/data/params_before_opt.json', 'w') as f:
    json.dump(saved, f, indent=2)
print('Saved:', saved)
"

# ── 1. Optimization (quick grid; switch to full once runtime is proven) ──
log "Step 1: Running optimizer v2 (quick grid, 2 windows)"
$VENV "$ROOT/scripts/optimize_system.py" --quick 2>&1 | tee "$LOG_DIR/optimize_$(date +%Y%m%d_%H%M).log"

# ── 2. Verify: live params must equal EITHER the pre-sweep snapshot (no
#      winner) or the applied best combo — anything else means the restore
#      or the apply half-failed. ──
log "Step 2: Verifying live params are consistent"
$VENV -c "
import json, sys
sys.path.insert(0, '$ROOT')
from src import runtime_config
d = json.load(open('$ROOT/data/optimized_params.json'))
saved = json.load(open('$ROOT/data/params_before_opt.json'))
live = {k: float(runtime_config.current(k))
        for k in ['entry_threshold', 'tp_atr_mult', 'sl_atr_mult']}
if d.get('applied') and d.get('best'):
    b = d['best']
    expect = {'entry_threshold': float(b['th']), 'tp_atr_mult': float(b['tp']),
              'sl_atr_mult': float(b['sl'])}
    src = 'applied best combo'
else:
    expect = {k: float(v) for k, v in saved.items()}
    src = 'pre-sweep snapshot (no change)'
for k, v in expect.items():
    if live[k] != v:
        print(f'CRITICAL: live {k}={live[k]} != expected {v} ({src}) — fix manually!')
        sys.exit(1)
print(f'OK: live params match {src}: {live}')
"

# ── 3. Report via the bot's own notifier (Telegram if configured, else log).
#      The auto-apply itself already sent its own Telegram; this is the
#      weekly one-line summary that fires even when nothing changed. ──
log "Step 3: Sending weekly summary"
$VENV -c "
import json, sys
sys.path.insert(0, '$ROOT')
d = json.load(open('$ROOT/data/optimized_params.json'))
best = d.get('best') or {}
base = d.get('baseline') or {}
msg = (
    '🔧 *Weekly Optimizer v2*\n'
    f\"Applied: {'YES' if d.get('applied') else 'no change'}\n\"
    f\"Best: THR={best.get('th','—')} TP={best.get('tp','—')} SL={best.get('sl','—')} \"
    f\"agg={best.get('agg_score','—')}\n\"
    f\"Baseline agg={base.get('agg_score','—')} | combos={d.get('combos_tested','?')} \"
    f\"| windows={len(d.get('windows', []))} | {d.get('elapsed_sec','?')}s\"
)
from src import notifier
notifier.send(msg)
print(msg)
"

log "DONE"
