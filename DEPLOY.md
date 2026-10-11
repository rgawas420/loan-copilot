# Deploying Urban Money Loan Copilot

One Linux server running two containers: the app, and Caddy in front of it for HTTPS. About 30 minutes.

```text
internet ──443──> Caddy (HTTPS, certificates renew automatically) ──> app:8000 (FastAPI + React, 1 process)
                                                                         ├── ./storage   databases, index, attachments
                                                                         ├── ./kb        knowledge-base documents
                                                                         └── Groq, Pinecone, Tavily, Razorpay (APIs)
```

## 1. What you need

| | |
|---|---|
| Server | Ubuntu 22.04/24.04, **2 vCPU, 8 GB RAM** (4 GB works but is tight: embeddings, reranker and OCR run in the app), 40 GB disk. Any provider: AWS Lightsail / EC2, DigitalOcean, Hetzner, Azure, a local VM. |
| Domain | e.g. `copilot.yourcompany.com`, with an **A record** pointing at the server's public IP. |
| Ports | 22 (SSH), 80 and 443 open. Nothing else. |
| Keys | Groq, **Pinecone** (required in production: the in-memory store re-embeds every document on each restart, which takes 30+ minutes for this knowledge base), Tavily (optional), Razorpay (for paid plans). |

## 2. Prepare the server

```bash
ssh ubuntu@YOUR_SERVER_IP
curl -fsSL https://get.docker.com | sudo sh          # Docker + the compose plugin
sudo usermod -aG docker $USER && newgrp docker
sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw --force enable
```

## 3. Copy the application

From your PC (PowerShell, in the folder that contains the project), or with `git clone` if it is in a repository:

```powershell
scp -r .\Urban_Money_Loan_Copilot ubuntu@YOUR_SERVER_IP:/opt/urban-money
```

`venv`, `node_modules`, `.env`, `var/` and the databases are not needed on the server (the image builds its own).

Put the knowledge-base documents in `kb/` and make the data folders writable by the app (it runs as user 1000):

```bash
cd /opt/urban-money
mkdir -p kb storage backups
cp -r "data/URBAN MONEY/." kb/        # or upload them later from Admin > Knowledge base
sudo chown -R 1000:1000 kb storage
```

## 4. Configure

```bash
cp .env.production.example .env
nano .env
```

Fill in at least `DOMAIN`, `ADMIN_API_KEY` (a long random string), `ADMIN_EMAILS` (your email), `GROQ_API_KEY`,
`PINECONE_API_KEY`, and for real payments `PAYMENT_PROVIDER=razorpay` with the three Razorpay values.
Use **new** API keys: the keys in the development `.env` / `.env.example` have been in files on a PC and in chat
screenshots; rotate them in the Groq, Pinecone and Tavily dashboards.

## 5. Start

```bash
docker compose up -d --build          # first build: ~10 minutes (downloads Python packages and models)
docker compose logs -f app            # watch the first start: knowledge-base sync, then "Knowledge base ready"
```

Open `https://YOUR_DOMAIN`. Caddy gets the certificate on the first request (needs ports 80/443 and the DNS record).

**Then immediately sign up with the email in `ADMIN_EMAILS`**: that account becomes admin. With
`ALLOW_REGISTRATION=false`, everyone else is added from *Admin console > Users*.

The first start indexes every document into Pinecone (and OCRs scanned files once); progress is in
*Admin console > Knowledge base*. Later starts only process new or changed files.

## 6. Payments (Razorpay)

In the Razorpay dashboard, add a webhook to `https://YOUR_DOMAIN/api/webhooks/payment` with the secret you put
in `RAZORPAY_WEBHOOK_SECRET`. Test a checkout in Razorpay test mode before switching to live keys.

## 7. Backups

```bash
chmod +x deploy/backup.sh
crontab -e
# add:  30 2 * * * cd /opt/urban-money && ./deploy/backup.sh >> backups/backup.log 2>&1
```

This keeps two weeks of nightly copies of the databases (accounts, chats, usage, billing, audit), attachments, ad
images and the knowledge-base documents. Copy `backups/` off the server too (S3, another machine): a backup on
the same disk does not survive losing the server.

## 8. Updating

```bash
cd /opt/urban-money
# copy the new code over (scp / git pull), keeping .env, kb/ and storage/
docker compose up -d --build
```

Databases migrate themselves on start. A change to models, prompt, reranker or chunking re-runs the golden tests
(*Admin console > RAG quality*); check them after an update.

## Useful commands

| | |
|---|---|
| Status and health | `docker compose ps` (the app shows `healthy`) |
| Logs | `docker compose logs -f app` / `docker compose logs -f caddy` |
| Restart | `docker compose restart app` |
| Stop | `docker compose down` (data in `storage/` and `kb/` stays) |
| Shell in the app | `docker compose exec app bash` |

## Things to know

- **One app process.** Rate limits, the knowledge-base job queue and SQLite are in-process, so run one container
  with one worker (as configured). This comfortably serves a few hundred partners. Growing beyond that means
  moving to PostgreSQL and a shared queue first.
- **Structured lender policies** (`.yaml` uploaded under *Policies and audit*) are written into the image's
  `data/policies`. To keep uploads across rebuilds, commit them to `data/policies/` before building, or add
  `- ./policies:/srv/data/policies` to the app's volumes (copy the existing files into `./policies` first).
- **Privacy.** Chats, attachments and the admin question log (`RAG_DEBUG_LOG`) hold customer case details. Keep
  the server patched (`sudo apt update && sudo apt upgrade`), restrict SSH to keys, and limit who is admin.
