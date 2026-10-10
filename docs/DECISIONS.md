# DECISIONS.md — Decision Records

Format: options · choice · why · who decided · date.

| ID | Decision | Options | Choice | Why | Decided by | Date |
|---|---|---|---|---|---|---|
| D-001 | Environment and dependency tool | uv · pip + pip-tools | **uv** | One free tool for venv, lock file, installs, and Python itself | Igor | 2026-10-10 |
| D-002 | CLI library | argparse · typer | **argparse** | Built into Python; one less dependency | Igor | 2026-10-10 |
| D-003 | Budget | $0 + Max credits · small prepaid budget | **$0 cash, Max 5x API credits only** ($100/mo); $5 Console spend limit as backup; no purchased credits, no auto-reload | Credits stop when empty and are never charged to the plan, so they act as a free hard cap | Igor | 2026-10-10 |
| D-004 | Max plan | — | **Max 5x** (fact recorded, not a choice) | Determines the $100/mo API credit | Igor | 2026-10-10 |
| D-005 | Repo visibility | public · private | **Public** | Portfolio piece; free Actions on public repos. Commits use GitHub's noreply email | Igor | 2026-10-10 |
| D-006 | Test video | own recording · Wikimedia Commons CC · Internet Archive CC | **Open** — Igor to pick from MVP.md section 4 | Rights must be clean even for private testing; no YouTube downloads | Igor | pending |
| D-007 | Where Claude Code and the app run | native Windows · WSL2 | **Native Windows** | 16 GB RAM is already ~86% used, and WSL2 adds a Linux VM on top. NVIDIA GPU setup is simpler on native Windows. Cost: no command sandbox natively [fact], so Manual permission mode is set in project settings, bypass and auto modes are disabled, and Igor reads every command | Claude proposed; Igor approved | 2026-10-10 |
| D-008 | How Igor works with Claude Code | Desktop app Code tab · terminal CLI only · VS Code + Claude Code extension + CLI | **VS Code + extension + CLI** | Editor, plan review, inline diffs and terminal in one window; Igor reads every diff. Settings and project memory are shared across surfaces [fact], so nothing is locked in | Claude proposed; Igor approved | 2026-10-10 |
| D-009 | Claude Code model per task | Opus only · Sonnet only · Fable · opusplan (terminal-only alias) · plan with Opus and build with Sonnet, switched by hand in VS Code | **Plan: Opus 5.5 · Build: Sonnet 5.5 · Effort: Medium** unless the brief says otherwise. Every brief states its models at the top. Never Fable (usage credits = extra money) | Best model where mistakes are cheapest to catch (the plan); protects the Max 5x weekly limit; `opusplan` isn't offered in the VS Code picker | Claude proposed; Igor approved | 2026-10-10 |
