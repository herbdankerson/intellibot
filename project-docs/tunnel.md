Perfect — that makes it even simpler.

If your only reason for Cloudflare is the lack of a static IP, stick with **Cloudflare Tunnel + SSH over Access**. It’ll punch through any network and doesn’t need a public IP.

Here’s the *fast-path setup*:

---

### **1. Ubuntu desktop**

```bash
sudo apt install -y openssh-server cloudflared
cloudflared tunnel login        # opens browser once; use your CF account
cloudflared tunnel create ssh-tunnel
```

Then edit:

```bash
sudo nano ~/.cloudflared/config.yml
```

Put this in:

```yaml
tunnel: ssh-tunnel
credentials-file: /home/$USER/.cloudflared/<id>.json

ingress:
  - hostname: ssh.<yourdomain>.com
    service: ssh://localhost:22
  - service: http_status:404
```

Run it:

```bash
cloudflared tunnel route dns ssh-tunnel ssh.<yourdomain>.com
cloudflared tunnel run ssh-tunnel
```

You now have `ssh.<yourdomain>.com` reachable via Cloudflare’s network — no public IP needed.

---

### **2. Windows laptop**

Install `cloudflared`:

```powershell
winget install Cloudflare.cloudflared
```

Then connect:

```powershell
cloudflared access tcp --hostname ssh.<yourdomain>.com --url localhost:2222
ssh youruser@localhost -p 2222
```

---

### **That’s all.**

No open ports, no static IP. If you want to persist it:

```bash
sudo systemctl enable --now cloudflared
```

This approach keeps everything inside Cloudflare’s tunnel, gives you CLI access anywhere, and plays fine with their free plan.
