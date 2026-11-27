"use strict";

const byId = id => document.getElementById(id);
const queue = [];
const eastern = new Intl.DateTimeFormat("en-US", {
  dateStyle: "medium", timeStyle: "short", timeZone: "America/New_York", hour12: true
});
let session, head, busy = false;
const publicationLabels = {
  publishing: "Publishing",
  verifying: "Verifying",
  published: "Successfully Published",
  partial: "Partially Published",
  failed: "Confirmed Failure"
};

function text(tag, content, className) {
  const element = document.createElement(tag);
  element.textContent = content;
  if (className) element.className = className;
  return element;
}

function button(label, handler, disabled = false) {
  const element = text("button", label);
  element.disabled = disabled;
  element.addEventListener("click", handler);
  return element;
}

function link(label, url) {
  const element = text("a", label);
  element.href = url;
  element.target = "_blank";
  element.rel = "noopener noreferrer";
  return element;
}

function notice(message) {
  byId("notice").textContent = message;
}

async function api(path, body, headers = {}, timeout) {
  const settings = {cache: "no-store", credentials: "same-origin"};
  if (body !== undefined) {
    const file = body instanceof File;
    settings.method = "POST";
    settings.headers = {"Content-Type": file ? "application/zip" : "application/json",
      "X-CSRF-Token": session.csrf, ...headers};
    settings.body = file ? body : JSON.stringify(body);
  }
  const controller = new AbortController();
  settings.signal = controller.signal;
  const timer = timeout && setTimeout(() => controller.abort(), timeout);
  try {
    return await request(path, settings);
  } finally {
    clearTimeout(timer);
  }
}

async function request(path, settings) {
  const response = await fetch("/api/" + path, settings);
  if (response.status === 401) {
    signedOut("✕ Session expired. Sign in again.");
    throw new Error("Session expired.");
  }
  if (response.status === 204) return;
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed.");
  return result;
}

function signedOut(message) {
  session = null;
  byId("connection").textContent = message;
  byId("workspace").hidden = byId("refresh").hidden = byId("logout").hidden = true;
  byId("login").hidden = false;
}

async function run(operation) {
  if (busy) return;
  busy = true;
  notice("");
  render();
  try {
    await operation();
  } catch (error) {
    notice("✕ " + error.message);
  } finally {
    busy = false;
    render();
  }
}

function add(files) {
  if (!session || busy) return;
  for (const file of files) {
    if (queue.length >= 20) {
      notice("Queue limit reached. Remove a package to add more.");
      break;
    }
    queue.push({file, channel: "auto", selected: true, message: "○ Waiting for validation"});
  }
  render();
}

function render() {
  const list = byId("queue");
  list.replaceChildren();
  for (const item of queue) {
    const row = document.createElement("li");
    const heading = text("h3", item.file.name);
    row.append(heading, text("p", (item.file.size / 1048576).toFixed(2) + " MiB"));
    const label = text("label", "Select package");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = item.selected;
    checkbox.disabled = busy || Boolean(item.publication);
    checkbox.addEventListener("change", () => { item.selected = checkbox.checked; controls(); });
    label.prepend(checkbox);
    row.append(label);
    const channel = document.createElement("select");
    channel.setAttribute("aria-label", "Release channel for " + item.file.name);
    for (const name of ["auto", "stable", "alpha", "beta"]) {
      channel.append(new Option(name === "auto" ? "Detect channel" : name[0].toUpperCase() + name.slice(1), name));
    }
    channel.value = item.channel;
    channel.disabled = busy || Boolean(item.publication);
    channel.addEventListener("change", () => {
      item.channel = channel.value;
      item.preview = null;
      item.message = "○ Channel changed. Validate again.";
      item.error = false;
      render();
    });
    row.append(channel);
    const tone = item.error || item.status === "failed" ? "error" : item.download ? "good" : "warn";
    const status = text("p", item.message, tone);
    row.append(status);
    if (item.preview) {
      const review = item.preview;
      heading.textContent = review.name + " · " + review.version;
      row.append(text("p", review.addon === "ShadyDesktopManager" ? "Manager Update" : review.addon + " · " + review.channel));
      if (!review.alreadyPublished) {
        row.append(text("p", "Current version " + (review.current || "none") + " · " + review.destination));
        if (review.catalogChange) row.append(text("p", "Catalog update expected"));
      }
      for (const note of [...review.changelog, ...review.features]) row.append(text("p", "• " + note));
      for (const warning of review.warnings) row.append(text("p", "⚠ " + warning, "warn"));
    }
    const actions = text("div", "", "actions");
    if (item.publication) {
      if (item.commit) actions.append(link("View GitHub Commit", item.commit));
      if (item.download) actions.append(link("View Published Release", item.download));
      actions.append(button("Verify Publication", () => run(async () => { await verify(item); if (session) await refresh(); }), busy));
    }
    if (!item.publication || item.retryable) {
      actions.append(button(item.publication ? "Retry · Validate Again" : "Validate Package", () => run(() => validate(item)), busy));
    }
    actions.append(button("Remove From Queue", () => {
      queue.splice(queue.indexOf(item), 1);
      render();
    }, busy));
    row.append(actions);
    list.append(row);
  }
  controls();
}

function controls() {
  const valid = queue.filter(item => item.preview && !item.publication && !item.error);
  byId("count").textContent = queue.length + " queued";
  byId("empty").hidden = queue.length > 0;
  byId("all").disabled = busy || !valid.length;
  byId("selected").disabled = busy || !valid.some(item => item.selected);
  for (const id of ["browse", "logout", "refresh"]) byId(id).disabled = busy;
}

async function validate(item) {
  item.message = "↑ Uploading ZIP for server validation";
  item.error = false;
  item.preview = null;
  render();
  try {
    item.preview = await api("validate", item.file, {"X-Release-Channel": item.channel});
    if (item.preview.alreadyPublished) {
      item.publication = reference(item.preview);
      await verify(item, true);
    } else {
      item.publication = null;
      item.status = null;
      item.retryable = false;
      item.commit = null;
      item.download = null;
      item.message = "✓ Validated · awaiting confirmation";
    }
  } catch (error) {
    item.error = true;
    item.message = "✕ " + error.message;
  }
  render();
}

function reference(review) {
  const {addon, channel, version, sha256, managerFields} = review;
  return {addon, channel, version, sha256: managerFields?.sha256 ?? sha256};
}

function updatePublication(item, status, detail) {
  item.status = status;
  item.message = publicationLabels[status] + " · " + detail;
  byId("publication-progress").textContent = (item.preview?.name || item.publication.addon) + " " + item.publication.version + " · " + item.message;
  render();
}

async function verify(item, automatic = false, publishError = "") {
  const delays = automatic ? [0, 2000, 5000, 8000] : [0, 2000];
  let absent = 0;
  item.error = false;
  for (const [index, delay] of delays.entries()) {
    if (delay) await new Promise(resolve => setTimeout(resolve, delay));
    updatePublication(item, "verifying", "Checking GitHub metadata and the public download.");
    let result;
    try {
      result = await api("verify-publication", item.publication, {}, 120000);
    } catch (error) {
      result = {status: "verifying", detail: session
        ? "Verification could not reach the service. Use Verify Publication to check again."
        : "Sign in again to verify publication."};
    }
    absent = result.status === "absent" ? absent + 1 : 0;
    item.download = result.download;
    item.commit = item.commit || result.link;
    item.retryable = false;
    if (result.status === "absent") {
      if (absent < 2 || (automatic && index < delays.length - 1)) {
        updatePublication(item, "verifying", "Waiting for the publication to appear on GitHub.");
        continue;
      }
      result.status = "failed";
      if (publishError) result.detail += " Publish response: " + publishError;
    }
    item.retryable = result.retryable === true;
    updatePublication(item, result.status, result.detail);
    if (result.status === "published") {
      setTimeout(() => {
        const index = queue.indexOf(item);
        if (index !== -1 && item.status === "published") {
          queue.splice(index, 1);
          render();
        }
      }, 3000);
      return;
    }
    if (result.status === "failed" || !session ||
        (result.status === "partial" && (item.retryable || !automatic))) return;
  }
}

async function publish(all) {
  const items = queue.filter(item => item.preview && !item.publication && !item.error && (all || item.selected));
  const summary = items.map(item => {
    const {addon, version, channel, destination} = item.preview;
    return addon + " " + version + " (" + channel + ") → " + destination;
  }).join("\n");
  if (!items.length || !window.confirm("Publish the following packages to the public repository?\n\n" + summary)) return;
  for (const [index, item] of items.entries()) {
    if (!session) break;
    item.publication = reference(item.preview);
    updatePublication(item, "publishing", "Uploading ZIP and saving to GitHub (" + (index + 1) + " of " + items.length + ").");
    let publishError = "";
    try {
      const receipt = await api("publish", item.file, {
        "X-Validation-Ticket": item.preview.ticket, "X-Release-Confirmation": "publish"
      }, 150000);
      if (receipt.status === "verification_pending") item.commit = receipt.link;
      if (receipt.status === "push_unconfirmed") publishError = "GitHub did not acknowledge the push attempts.";
    } catch (error) {
      if (error.name !== "AbortError" && !(error instanceof TypeError) && !(error instanceof SyntaxError)) {
        publishError = error.message;
      }
    }
    if (session) await verify(item, true, publishError);
  }
  if (session) await refresh();
}

async function rollback(row) {
  if (!window.confirm("Restore " + row.addon + " " + row.version + " in the " + row.channel + " channel? Published ZIPs will be retained.")) return;
  const result = await api("rollback", row, {"X-Reviewed-Head": head, "X-Release-Confirmation": "rollback"});
  try {
    await api("check", result);
    notice("✓ Rollback verified for " + row.addon + " " + row.version);
  } catch (error) {
    notice("⚠ Rollback verification pending. Refresh history or check the resulting commit. " + error.message);
    byId("notice").append(" ", link("View Commit", result.link),
      button("Retry Verification", () => run(async () => {
        await api("check", result);
        notice("✓ Rollback verified for " + row.addon + " " + row.version);
      })));
  }
  await refresh();
}

async function refresh() {
  byId("connection").textContent = "○ Checking repository";
  try {
    const result = await api("history");
    head = result.head;
    byId("connection").textContent = "✓ Connected to ShadyInc-Addons";
    byId("connection").className = "good";
    byId("history-state").textContent = "Latest commits with release records";
    const list = byId("history");
    list.replaceChildren();
    for (const row of result.rows) {
      const element = document.createElement("li");
      element.append(text("h3", row.addon + " " + row.version + " · " + row.channel),
        text("p", "✓ Committed · " + row.action + " · " + eastern.format(new Date(row.date)) + " ET · " + row.source),
        link("View Commit", row.link),
        " ", button("Verify Publication", () => run(async () => {
          notice("Verifying · " + row.addon + " " + row.version);
          try {
            const result = await api("check", row);
            notice("Successfully Published · " + row.addon + " " + row.version);
            byId("notice").append(" ", link("View Published Release", result.download));
          } catch (error) {
            notice("Verification incomplete · " + error.message);
          }
        })));
      if (row.addon !== "ShadyDesktopManager") {
        element.append(" ", button("Restore This Version", () => run(() => rollback(row))));
      }
      list.append(element);
    }
  } catch (error) {
    byId("connection").textContent = "✕ Repository unavailable";
    byId("connection").className = "error";
    byId("history-state").textContent = error.message;
    byId("history").replaceChildren();
  }
}

const picker = byId("files");
byId("browse").onclick = () => picker.click();
picker.onchange = () => { add(picker.files); picker.value = ""; };
const drop = byId("drop");
drop.ondragover = event => { event.preventDefault(); drop.classList.add("drag"); };
drop.ondragleave = () => drop.classList.remove("drag");
drop.ondrop = event => { event.preventDefault(); drop.classList.remove("drag"); add(event.dataTransfer.files); };
byId("selected").onclick = () => run(() => publish(false));
byId("all").onclick = () => run(() => publish(true));
byId("refresh").onclick = () => run(refresh);
byId("logout").onclick = () => run(async () => {
  await api("logout", {});
  queue.length = 0;
  signedOut("○ Signed out");
});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
run(async () => {
  session = await api("session");
  if (!session.configured) {
    signedOut("○ GitHub App setup required");
    byId("login").hidden = true;
  } else if (!session.authenticated) {
    signedOut("○ Sign in to connect");
  } else {
    byId("workspace").hidden = byId("refresh").hidden = byId("logout").hidden = false;
    await refresh();
  }
});
