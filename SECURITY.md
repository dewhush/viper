# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in VIPER, please report it **privately**:

- **Email**: security@dewhush.dev (or via GitHub private security advisory)
- **Do NOT open a public issue** for security vulnerabilities.

### What to include

- Description of the vulnerability
- Steps to reproduce (if possible)
- Potential impact assessment
- Suggested fix (if you have one)

### Response timeline

- **Acknowledgment**: Within 48 hours
- **Initial assessment**: Within 7 days
- **Resolution**: Within 30 days (depending on severity)

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.1.x   | ✅ Current |
| < 1.0   | ❌ End-of-life |

## Security Best Practices

When running VIPER:

1. **Never commit `.env` to Git** — use `.env.example` as a template
2. **Encrypt credentials** at rest using `viper_env.py` (AES-256-CBC)
3. **Restrict file permissions**: `chmod 600 .env`
4. **Use dedicated API keys** with minimum required permissions (Order + Position only)
5. **Set IP whitelist** on exchange API keys if possible
6. **Run on isolated VPS** with firewall (no public ports except SSH)
7. **Rotate API keys** every 90 days
8. **Monitor audit log** regularly for unauthorized access attempts

## Threat Model

VIPER is designed to be deployed on a private server (VPS) with no public-facing services. It communicates with:

- **Bybit API** (REST + WebSocket) over HTTPS/WSS
- **Telegram Bot API** over HTTPS (outbound only)
- **Local LLM endpoint** (localhost, no internet exposure)

The `.env` file containing API keys is encrypted at rest using AES-256-CBC with a passphrase derived via PBKDF2.
