# Shady Remote Publisher v1.0.0

Installable iPhone publisher for WoW Forever addon ZIPs. This service runs independently of the Windows PC. The production repository and GitHub Pages website retain their existing structure. The deployment branch can be deployed directly without merging into `main`.

## Setup from an iPhone

1. In Safari, open the [Render dashboard](https://dashboard.render.com/) and create a **Web Service**. Use the GitHub repository `ianpgh/ShadyInc-Addons` and the `feature/shady-remote-publisher` branch. Connect GitHub if prompted, granting repository access only to this project. The service can be created before auth is configured. For a test deployment select **Free**, **Docker**, root directory `remote-publisher`, and one instance. Leave Docker command and Dockerfile path at their defaults. Disable automatic deploys if you want to deploy only reviewed commits. Render assigns a unique HTTPS URL. Free services sleep and can lose in-memory sessions.
2. In [GitHub App settings](https://github.com/settings/apps/new), create a private GitHub App in your personal account, accessible **Only on this account**. Use the Render HTTPS origin as the homepage and set the callback URL to that origin plus `/oauth/callback`. Clear the Webhook **Active** checkbox. Leave device flow and install-time OAuth callbacks disabled. Keep expiring user tokens enabled.
3. Under repository permissions, set **Contents** to **Read and write** and leave **Metadata** at its required read-only setting. Leave all other permissions unset. Install the app on **only** `ianpgh/ShadyInc-Addons`. This is separate from Render’s GitHub integration.
4. In the GitHub App settings copy its **Client ID** and generate a **Client secret**. In Render's environment settings add `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and `PUBLIC_ORIGIN` (exact assigned HTTPS origin with no suffix). Store the client secret only on the hosting provider. The app needs no PAT, webhook secret, or GitHub App private key.
5. Deploy and open the HTTPS service. Sign in as `ianpgh`, use **Refresh Repository**, then validate a candidate ZIP without publishing it. Check that an unrelated GitHub account has no publishing access. Keep production releases untouched until explicitly approved.
6. In Safari, use **Share → Add to Home Screen → Open as Web App → Add**. Open the new icon and sign in from the web app if Safari's session is separate.

Use one container replica and an HTTPS reverse proxy with request time and body limits. The proxy must preserve the request Origin and forward a Content-Length for uploads. A host restart or deployment expires all sessions and validation tickets. Sign in and validate again after a restart. Use the assigned service URL as the direct mobile app URL.

## Publishing and recovery

- Uploading and validating sends data only to the service. The queue stays in memory. The server inspects the exact bytes again when you confirm publication. A compatible manual channel selection must agree with the TOC version.
- Each package is its own commit. Stable uses `latest.json`, Alpha uses `alpha.json`, and Beta uses `beta.json`. The ZIP keeps the desktop publisher's filename and checksum format. A failed package does not prevent other packages from being attempted.
- The server compares the live ref before commit and uses a non-force push. If Windows publishes first, refresh and validate again. If an upload or response drops, refresh history before retrying. Verification checks the commit, current raw feed, GitHub Pages feed and download, and the public catalog for Stable. A pending result can be rechecked while Pages propagates.
- History shows matching release commits with author/source and Eastern time. Restore an earlier version from the same channel and confirm explicitly. Rollback commits only the selected manifest, retains all ZIPs, and requires the reviewed head to remain current. Catalog descriptions and features remain as currently published.
- The remote service accepts addon ZIPs only. Keep using the Windows publisher for the Desktop Addon Manager executable or a publisher-specific bundle.
- Sign-out deletes the server-side session. For emergency revocation, uninstall the GitHub App or revoke its authorization in GitHub, rotate the client secret if exposed, and stop or redeploy the service. Git history records successful mutations and the service emits structured operation logs. Host log retention is dependent on the selected plan.

## Local checks

Install Git, Python 3.10+ and Lua 5.1. Run `python3 tests.py` in this directory. If the compiler is installed at a different path, set `TEST_LUAC` to its absolute path. `node --check web/app.js` checks the client script. Read [AUDIT.md](AUDIT.md) for repository verification, test coverage and release gates.
