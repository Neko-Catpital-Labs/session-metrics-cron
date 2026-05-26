# Source Host Onboarding

## Local source

Set local paths in `config/sources.json`:

- `local.codexSessionsDir`
- `local.claudeRootDir`

## SSH source

Add an entry under `remoteTargets`:

```json
{
  "name": "ssh-source-1",
  "enabled": true,
  "host": "my-host.example",
  "user": "invoker",
  "port": 22,
  "sshKeyPath": "~/.ssh/id_ed25519",
  "codexSessionsDir": "/home/invoker/.codex/sessions",
  "claudeRootDir": "/home/invoker/.claude"
}
```

## Connectivity check

Before nightly runs, verify SSH/rsync manually:

```bash
ssh -i ~/.ssh/id_ed25519 -p 22 invoker@my-host.example 'echo ok'
```

If this fails, fix host/user/key first; the audit step depends on `ssh` and `rsync`.
