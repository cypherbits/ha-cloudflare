Home Assistant integracion for Cloudflare DNS that replaces the core HA integration.
Adds multiple zones, A record selection and Proxy bypass switch.

Once a zone is configured you can add more domains/subdomains at any time through
**Configuración > Dispositivos y servicios > Cloudflare > Configurar** (options flow).
Select existing A records or type new subdomains (comma-separated); records that do not
exist yet are created automatically pointing to your external IP.