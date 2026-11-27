# Production audit and verification

Baseline commit `80454fa0534cc305a5640eec82d3de4c02859ebe` is preserved on [recovery/pre-remote-publisher](https://github.com/ianpgh/ShadyInc-Addons/tree/recovery/pre-remote-publisher).

Production consists of the root `index.html`, `catalog.json`, and versioned files and manifests below `releases/`. GitHub REST reported no GitHub Release objects and one active, GitHub-managed Pages deployment workflow. The site and Windows manager use repository files. The site consumes the catalog and stable `latest.json` files and checks that the asset exists. Older manually uploaded Alpha and Beta artifacts remain in Git history. There were no active `alpha.json` or `beta.json` files at the baseline.

The latest located Windows Publisher source was 1.8. It checks the root and TOC, preserves ZIP bytes, names the asset from the exact TOC version, generates SHA-256, updates a channel manifest, and pushes a single non-force commit from a new clone. Stable uses `latest.json`, Alpha uses `alpha.json`, and Beta uses `beta.json`. The Windows publisher may treat an identical re-upload as success. The remote publisher rejects duplicate versions explicitly, including equivalent spellings of retained versioned assets.

The Desktop Addon Manager 0.21 P0 preview includes Go source. Its executable matches the published 0.21.0 executable byte-for-byte. Its catalog parser reads the existing catalog fields, recognizes separate prerelease channel manifests, and selects Stable by default. The remote publisher preserves those formats and validates the generated asset checksum. Windows GUI execution was outside this Linux environment.

The public index and catalog matched the baseline bytes. Each public manifest matched its repository copy. All 17 public assets downloaded and matched SHA-256.

| Addon or app | Version | Public download |
| --- | --- | --- |
| ActionHero | 2.2.1 | [ActionHero_v2.2.1.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/ActionHero/ActionHero_v2.2.1.zip) |
| AuctionCompanion | 1.2.3 | [AuctionCompanion_v1.2.3.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/AuctionCompanion/AuctionCompanion_v1.2.3.zip) |
| AuctionPeek | 2.9.3 | [AuctionPeek_v2.9.3.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/AuctionPeek/AuctionPeek_v2.9.3.zip) |
| BuffHero | 1.6.3 | [BuffHero_v1.6.3.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/BuffHero/BuffHero_v1.6.3.zip) |
| CampfireSongs | 1.0.6 | [CampfireSongs_v1.0.6.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/CampfireSongs/CampfireSongs_v1.0.6.zip) |
| EasyFollow | 1.7.5 | [EasyFollow_v1.7.5.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/EasyFollow/EasyFollow_v1.7.5.zip) |
| GrindTracker | 7.0.0 | [GrindTracker_v7.0.0.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/GrindTracker/GrindTracker_v7.0.0.zip) |
| HideThisManager | 1.3.6 | [HideThisManager_v1.3.6.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/HideThisManager/HideThisManager_v1.3.6.zip) |
| MiniMappin | 1.3.1 | [MiniMappin_v1.3.1.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/MiniMappin/MiniMappin_v1.3.1.zip) |
| PinMe | 1.2.3 | [PinMe_v1.2.3.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/PinMe/PinMe_v1.2.3.zip) |
| ProfessionAIO | 1.2.0 | [ProfessionAIO_v1.2.0.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/ProfessionAIO/ProfessionAIO_v1.2.0.zip) |
| ShadyDesktopManager | 0.21.0 | [ShadyAddonManager_0.21.0_windows_amd64.exe](https://ianpgh.github.io/ShadyInc-Addons/releases/ShadyDesktopManager/ShadyAddonManager_0.21.0_windows_amd64.exe) |
| ShadyFlightTimer | 1.4.2 | [ShadyFlightTimer_v1.4.2.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/ShadyFlightTimer/ShadyFlightTimer_v1.4.2.zip) |
| ShadyIncAddonManager | 1.10.0 | [ShadyIncAddonManager_v1.10.0.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/ShadyIncAddonManager/ShadyIncAddonManager_v1.10.0.zip) |
| ShadyPriceEngine | 1.2.15 | [ShadyPriceEngine_v1.2.15.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/ShadyPriceEngine/ShadyPriceEngine_v1.2.15.zip) |
| StatGains | 1.9.3 | [StatGains_v1.9.3.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/StatGains/StatGains_v1.9.3.zip) |
| WritTracker | 4.0.2 | [WritTracker_v4.0.2.zip](https://ianpgh.github.io/ShadyInc-Addons/releases/WritTracker/WritTracker_v4.0.2.zip) |

## Isolated tests

- Thirteen included Python tests passed with Lua 5.1.5. They cover malformed or unsafe archives, nested archives, duplicate paths, binaries, compression limits, invalid metadata, missing TOC files, the Lua 60-upvalue compiler limit, unauthorized and cross-origin requests, OAuth owner mismatch and state replay, interrupted uploads, independent channels, duplicates and downgrades, live-head races, simulated push failure, and preservation of historical ZIPs during rollback.
- The original Windows Publisher 1.8 Go tests passed. Its unchanged Go publishing core was also exercised against the remote core on a local bare Git repository with synthetic packages. Tests covered both publisher orderings, a stale checkout, a duplicate version with different bytes, downgrade rejection and Alpha/Beta followed by Stable.
- Desktop Addon Manager 0.21 Go tests passed, including a check that its real catalog and channel feed parsers accept the remote publisher’s synthetic Stable, Alpha and Beta packages and verify their ZIP hashes.
- Chromium at an iPhone 13 viewport passed the queue, accessibility text indicators, cancel/confirm flow, safe rendering of package notes, and verified-download link checks against stubbed responses. The real server's signed-out view was used. Physical iPhone Safari installation and a live authentication callback remain deployment checks.
- The production Shady Price Engine 1.2.15 archive includes a `.bak` entry that the stricter remote validator rejects. Its existing published package remains valid against its baseline checksum and was not changed.

## Security review

- The GitHub App uses authorization code flow with PKCE, one-use state, expiration, and an immutable GitHub owner ID check. The server checks that the app installation is scoped to this repository. Sessions live in memory and expire after 30 minutes. The browser receives a secure HTTP-only session cookie and a separate CSRF token. Expiring GitHub tokens stay server-side and refresh tokens are discarded.
- Authenticated writes require a matching Origin, CSRF proof and explicit intent. Publish also requires a single-use validation ticket bound to the original ZIP and the reviewed active versions. Archives are read without extracting member paths, content is bounded and hashed, and the compiler parses Lua without executing package code. The compiler gets a minimal environment.
- Repository destinations are derived from validated TOC data. Symlinks in the destination path and manifest are refused. Git writes use a commit with specific release paths and a non-force push. The HTTPS app needs no Actions or workflow write permissions. The service emits structured logs, and permanent successful edits are traceable in Git.
- Hosting authentication, container build and page propagation after an authenticated production write remain unverified. The recommended free host is suitable for low-volume evaluation, with cold starts and no uptime guarantee. The single-instance service has in-memory sessions and conservative operation limits. Rollback changes a channel pointer while keeping catalog copy and historical downloads intact.

## Remote Publisher v1.1.0

The publisher writes the versioned asset, channel metadata, and any catalog changes in a single Git commit. A missing push response left the browser without the commit SHA needed by the original verification route. GitHub Pages can also serve an older manifest after GitHub has accepted a commit.

The publisher now checks GitHub after an uncertain push and makes one retry only after reviewing the current channel and matching checksums. Verification also accepts the validated package identity, so the browser can check a publication after a dropped request. ZIPs with conflicting bytes and versions retained after rollback are protected from overwrite. The public check verifies the commit, the current raw feed, GitHub Pages metadata, catalog presence, and the full download checksum.

The pre-update commit is preserved on [recovery/pre-publication-verification](https://github.com/ianpgh/ShadyInc-Addons/tree/recovery/pre-publication-verification). The update passed 22 Python regression tests and five Chromium scenarios covering a lost response, delayed Pages propagation and manual verification, repeated absence, GitHub check failure, and duplicate ZIP submission. A read-only check also verified the existing ActionHero 2.2.6 download against the published metadata.
