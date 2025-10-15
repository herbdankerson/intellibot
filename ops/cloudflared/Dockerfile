FROM python:3.12-slim

# Copy pre-installed cloudflared binary from host extraction
COPY cloudflared /usr/local/bin/cloudflared
RUN chmod +x /usr/local/bin/cloudflared

ENTRYPOINT ["cloudflared"]
CMD ["--help"]
