# Rotating secrets

| secret | how |
|---|---|
| API keys | create a new key (`fixloop-admin create-key`), switch the client, `fixloop-admin revoke-key <id>`; revocation takes effect within `api.key_cache_ttl_s` (60 s) |
| Gemini key | update the secret, restart workers; nothing else holds it |
| GitHub App private key | generate a second key in the App settings, update the publisher secret, restart, delete the old key |
| GitHub webhook secret | change it in the App settings and in the API secret at the same time; deliveries in between fail and GitHub retries them |
| S3 credentials | issue new keys scoped to the bucket, update every service, revoke the old ones |
| `api.upload_token_secret` | change and restart the API; outstanding local upload URLs stop working (they expire within an hour anyway) |
