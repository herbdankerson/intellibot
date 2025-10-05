# Cloudflare Tunnel Setup for dankersons.com

This guide walks through verifying the `cloudflared` CLI, creating tunnels for `dankersons.com`, and laying out a sample ingress config. Adapt the hostnames/services to match what you want to expose.

## 1. Verify CLI and login

```bash
cloudflared --version
cloudflared tunnel list
```

If CLI warns about being out of date, update via your package manager or the Cloudflare download page.

If you ever need to refresh the account certificate (for new domains), run:

```bash
cloudflared tunnel login
```

This opens a browser prompt so the cert includes `dankersons.com`.

## 2. Create tunnels

Pick a descriptive name per tunnel. Examples below assume:

- `dankersons-core` for web traffic to Caddy on Docker network
- `dankersons-rdp` for an RDP jump host
- `dankersons-lab` for miscellaneous lab services

```bash
cloudflared tunnel create dankersons-core
cloudflared tunnel create dankersons-rdp
cloudflared tunnel create dankersons-lab
```

Each command drops a credentials JSON (UUID) into `~/.cloudflared/`. Record the UUIDs for later.

## 3. Configure DNS routing

Map the tunnels to the hostnames you own under `dankersons.com`:

```bash
cloudflared tunnel route dns dankersons-core app.dankersons.com
cloudflared tunnel route dns dankersons-core portainer.dankersons.com
cloudflared tunnel route dns dankersons-rdp rdp.dankersons.com
cloudflared tunnel route dns dankersons-lab grafana.dankersons.com
```

Repeat per tunnel + hostname combo. The command creates CNAME records pointing to Cloudflare's tunnel endpoint.

## 4. Build a config file

Create `~/.cloudflared/dankersons-config.yml` (or keep configs alongside the repo for source control). Use the UUIDs that `cloudflared tunnel create` returned.

```yaml
tunnel: <UUID for dankersons-core>
credentials-file: /home/george-siha/.cloudflared/<UUID for dankersons-core>.json

ingress:
  - hostname: app.dankersons.com
    service: http://caddy:80
  - hostname: portainer.dankersons.com
    service: http://caddy:80
  - service: http_status:404
```

For the RDP or other tunnels, either:

1. Put each tunnel in its own config file (`dankersons-rdp.yml`) and run separate `cloudflared` processes, or
2. Use a single tunnel with multiple ingress entries (preferred for fewer processes) by pointing multiple hostnames to the same tunnel UUID.

Example of combining everything into one tunnel:

```yaml
tunnel: <UUID for dankersons-core>
credentials-file: /home/george-siha/.cloudflared/<UUID>.json

ingress:
  - hostname: app.dankersons.com
    service: http://caddy:80
  - hostname: portainer.dankersons.com
    service: http://caddy:80
  - hostname: rdp.dankersons.com
    service: rdp://localhost:3389
  - hostname: grafana.dankersons.com
    service: http://localhost:3000
  - service: http_status:404
```

## 5. Run the tunnel

```bash
cloudflared tunnel run --config ~/.cloudflared/dankersons-config.yml
```

For systemd setups, generate a service file:

```bash
cloudflared service install --config ~/.cloudflared/dankersons-config.yml
```

Reload the service as needed:

```bash
sudo systemctl enable --now cloudflared
sudo systemctl restart cloudflared
```

## 6. Clean up / manage

- `cloudflared tunnel delete <name>` removes a tunnel and its DNS records.
- `cloudflared tunnel info <name>` shows current connections.
- Keep the credentials JSON safe; anyone with it can run the tunnel.

## Troubleshooting

- `cloudflared tunnel list` should show `STATUS` with active connections once running.
- If DNS fails to resolve, double-check that Cloudflare's proxy is enabled on each CNAME record and the hostnames match what is in the config file.
- For services binding localhost, run `cloudflared` on the same machine or expose them over Docker network.
```
