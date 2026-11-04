/* Doxari front-end: progressive enhancement over working server-rendered forms. */
(function () {
  "use strict";

  const doc = document;
  const $ = (sel, root) => (root || doc).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || doc).querySelectorAll(sel));
  const CSRF = (doc.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const body = doc.body;

  /* ------------------------------------------------------------ helpers */
  function toast(message, type) {
    if (!message) return;
    let stack = $(".toast-stack");
    if (!stack) {
      stack = doc.createElement("div");
      stack.className = "toast-stack";
      body.appendChild(stack);
    }
    const kind = type === "error" ? "error" : type === "info" ? "info" : "success";
    const icon = kind === "error" ? "alert" : kind === "info" ? "info" : "check";
    const el = doc.createElement("div");
    el.className = "toast toast-" + kind;
    el.setAttribute("role", "status");
    el.innerHTML =
      '<svg class="icon" aria-hidden="true"><use href="/static/img/icons.svg#i-' + icon + '"></use></svg>' +
      '<div class="grow"></div>' +
      '<button class="toast-close" aria-label="Dismiss">' +
      '<svg class="icon icon-sm" aria-hidden="true"><use href="/static/img/icons.svg#i-close"></use></svg></button>';
    el.querySelector(".grow").textContent = message;
    stack.appendChild(el);
    const remove = () => {
      el.style.opacity = "0";
      el.style.transform = "translateY(6px)";
      setTimeout(() => el.remove(), 200);
    };
    el.querySelector(".toast-close").addEventListener("click", remove);
    setTimeout(remove, kind === "error" ? 8000 : 4200);
  }

  async function api(url, options) {
    const opts = Object.assign({ method: "POST" }, options || {});
    opts.headers = Object.assign(
      { "X-CSRF-Token": CSRF, "X-Requested-With": "XMLHttpRequest" },
      opts.headers || {}
    );
    if (opts.json !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    if (opts.form) {
      opts.body = opts.form;
      delete opts.form;
    }
    const response = await fetch(url, opts);
    let data = {};
    try {
      data = await response.json();
    } catch (e) {
      data = {};
    }
    if (!response.ok) {
      const error = new Error(data.error || "Something went wrong. Please try again.");
      error.status = response.status;
      error.code = data.code;
      error.data = data;
      throw error;
    }
    return data;
  }

  function debounce(fn, wait) {
    let timer;
    return function () {
      const args = arguments;
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  /* ------------------------------------------------------------ confirm dialog */
  const confirmDialog = (function () {
    let resolver = null;
    const dialog = $("#confirmDialog");
    if (!dialog) return () => Promise.resolve(window.confirm("Are you sure?"));
    const titleEl = $("#confirmTitle", dialog);
    const bodyEl = $("#confirmBody", dialog);
    const labelEl = $("#confirmLabel", dialog);
    const noteEl = $("#confirmNote", dialog);
    const okBtn = $("#confirmOk", dialog);

    okBtn.addEventListener("click", () => {
      dialog.close();
      if (resolver) resolver(true);
      resolver = null;
    });
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      dialog.close();
      if (resolver) resolver(false);
      resolver = null;
    });
    dialog.addEventListener("close", () => {
      if (resolver) resolver(false);
      resolver = null;
    });

    return function (options) {
      titleEl.textContent = options.title || "Are you sure?";
      bodyEl.textContent = options.body || "";
      labelEl.textContent = options.confirmLabel || "Confirm";
      if (noteEl) {
        noteEl.textContent = options.note || "";
        noteEl.hidden = !options.note;
      }
      okBtn.className = "btn " + (options.danger === false ? "btn-primary" : "btn-danger");
      dialog.showModal();
      return new Promise((resolve) => {
        resolver = resolve;
      });
    };
  })();

  /* ------------------------------------------------------------ flash -> toast */
  $$("[data-flash]").forEach(function (el) {
    toast(el.dataset.flash, el.dataset.flashType);
  });

  /* ------------------------------------------------------------ theme */
  (function initTheme() {
    const root = doc.documentElement;
    const stored = localStorage.getItem("doxari-theme");
    if (stored === "dark" || stored === "light") {
      root.dataset.theme = stored;
    } else if (!root.dataset.theme) {
      root.dataset.theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    $$("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", async () => {
        const next = root.dataset.theme === "dark" ? "light" : "dark";
        root.dataset.theme = next;
        localStorage.setItem("doxari-theme", next);
        doc.cookie = "doxari_theme=" + next + ";path=/;max-age=31536000;samesite=Lax";
        $$("[data-theme-icon]").forEach((icon) => {
          icon.setAttribute("href", "/static/img/icons.svg#i-" + (next === "dark" ? "sun" : "moon"));
        });
        try {
          await api("/api/preferences", { json: { theme: next } });
        } catch (e) {
          /* theme still applies locally */
        }
      });
    });
  })();

  /* ------------------------------------------------------------ shell */
  (function initShell() {
    $$("[data-nav-toggle]").forEach((button) => {
      button.addEventListener("click", () => body.classList.toggle("nav-open"));
    });
    doc.addEventListener("click", (event) => {
      if (body.classList.contains("nav-open") && !event.target.closest(".sidebar, [data-nav-toggle]")) {
        body.classList.remove("nav-open");
      }
    });

    // dropdowns
    $$("[data-dropdown-toggle]").forEach((toggle) => {
      const target = doc.getElementById(toggle.dataset.dropdownToggle);
      if (!target) return;
      toggle.addEventListener("click", (event) => {
        event.stopPropagation();
        const open = !target.hidden;
        $$(".dropdown").forEach((d) => (d.hidden = true));
        target.hidden = open;
        toggle.setAttribute("aria-expanded", String(!open));
      });
      doc.addEventListener("click", (event) => {
        if (!target.hidden && !event.target.closest(".dropdown, [data-dropdown-toggle]")) {
          target.hidden = true;
        }
      });
      doc.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !target.hidden) target.hidden = true;
      });
    });
  })();

  /* ------------------------------------------------------------ modals */
  $$("[data-modal-open]").forEach((trigger) => {
    trigger.addEventListener("click", (event) => {
      const dialog = doc.getElementById(trigger.dataset.modalOpen);
      if (!dialog) return;
      event.preventDefault();
      if (trigger.dataset.prefill) {
        try {
          const values = JSON.parse(trigger.dataset.prefill);
          Object.keys(values).forEach((key) => {
            const field = dialog.querySelector('[name="' + key + '"]');
            if (field) field.value = values[key];
          });
        } catch (e) {}
      }
      if (trigger.dataset.formAction) {
        const form = dialog.querySelector("form");
        if (form) form.setAttribute("action", trigger.dataset.formAction);
      }
      if (trigger.dataset.targetLabel) {
        const label = dialog.querySelector("[data-target-label]");
        if (label) label.textContent = trigger.dataset.targetLabel;
      }
      dialog.showModal();
      const first = dialog.querySelector("input:not([type=hidden]), select, textarea");
      if (first && window.innerWidth > 720) setTimeout(() => first.focus(), 60);
    });
  });
  $$("dialog [data-modal-close]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog").close());
  });
  $$("dialog.modal").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });

  /* ------------------------------------------------------------ global search */
  (function initSearch() {
    const input = $("#globalSearch");
    const box = $("#globalSuggestions");
    if (!input || !box) return;

    let controller = null;
    let index = -1;

    const render = (results, term) => {
      box.innerHTML = "";
      index = -1;
      if (!results.length) {
        box.innerHTML = '<div class="suggestion-empty">No matches for “' + term + '”</div>';
        return;
      }
      results.forEach((item) => {
        const link = doc.createElement("a");
        link.className = "suggestion";
        link.href = item.url;
        link.innerHTML =
          '<svg class="icon" aria-hidden="true"><use href="/static/img/icons.svg#i-' +
          (item.type === "folder" ? "folder" : "file") +
          '"></use></svg><div class="suggestion-body"><div class="suggestion-label"></div><div class="suggestion-meta"></div></div>';
        link.querySelector(".suggestion-label").textContent = item.label;
        link.querySelector(".suggestion-meta").textContent = item.meta;
        box.appendChild(link);
      });
    };

    const search = debounce(async () => {
      const term = input.value.trim();
      if (term.length < 2) {
        box.innerHTML = "";
        return;
      }
      if (controller) controller.abort();
      controller = new AbortController();
      try {
        const response = await fetch("/api/search/suggest?q=" + encodeURIComponent(term), {
          signal: controller.signal,
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        const data = await response.json();
        render(data.results || [], term);
      } catch (e) {
        /* aborted or offline */
      }
    }, 220);

    input.addEventListener("input", search);
    input.addEventListener("focus", () => {
      if (input.value.trim().length >= 2) search();
    });
    input.addEventListener("keydown", (event) => {
      const items = $$(".suggestion", box);
      if (event.key === "ArrowDown" && items.length) {
        event.preventDefault();
        index = Math.min(items.length - 1, index + 1);
        items.forEach((el, i) => el.setAttribute("aria-selected", String(i === index)));
        items[index].scrollIntoView({ block: "nearest" });
      } else if (event.key === "ArrowUp" && items.length) {
        event.preventDefault();
        index = Math.max(0, index - 1);
        items.forEach((el, i) => el.setAttribute("aria-selected", String(i === index)));
        items[index].scrollIntoView({ block: "nearest" });
      } else if (event.key === "Enter") {
        if (index >= 0 && items[index]) {
          event.preventDefault();
          window.location.href = items[index].href;
        }
      } else if (event.key === "Escape") {
        box.innerHTML = "";
        input.blur();
      }
    });
    doc.addEventListener("click", (event) => {
      if (!event.target.closest(".global-search")) box.innerHTML = "";
    });
    doc.addEventListener("keydown", (event) => {
      const tag = (event.target.tagName || "").toLowerCase();
      const typing = tag === "input" || tag === "textarea" || event.target.isContentEditable;
      if (event.key === "/" && !typing) {
        event.preventDefault();
        input.focus();
      }
    });
  })();

  /* ------------------------------------------------------------ notifications */
  (function initNotifications() {
    const button = $("[data-notifications]");
    const panel = $("#notificationPanel");
    if (!button || !panel) return;
    let loaded = false;
    button.addEventListener("click", async () => {
      if (panel.hidden === false) {
        panel.hidden = true;
        return;
      }
      panel.hidden = false;
      if (loaded) return;
      panel.innerHTML = '<div class="suggestion-empty">Loading…</div>';
      try {
        const data = await api("/api/notifications", { method: "GET" });
        panel.innerHTML = "";
        (data.alerts || []).forEach((alert) => {
          const item = doc.createElement("a");
          item.className = "dropdown-item";
          item.href = alert.url || "#";
          item.innerHTML =
            '<svg class="icon" aria-hidden="true"><use href="/static/img/icons.svg#i-' + (alert.icon || "info") +
            '"></use></svg><div><div class="strong"></div><div class="suggestion-meta"></div></div>';
          item.querySelector(".strong").textContent = alert.title;
          item.querySelector(".suggestion-meta").textContent = alert.detail;
          panel.appendChild(item);
        });
        if (!(data.items || []).length && !(data.alerts || []).length) {
          const empty = doc.createElement("div");
          empty.className = "suggestion-empty";
          empty.textContent = "Nothing yet. Your document activity will appear here.";
          panel.appendChild(empty);
        }
        (data.items || []).forEach((item) => {
          const link = doc.createElement("a");
          link.className = "dropdown-item";
          link.href = item.document_id ? "/documents/" + item.document_id : "/activity";
          link.innerHTML =
            '<svg class="icon" aria-hidden="true"><use href="/static/img/icons.svg#i-' + (item.icon || "file") +
            '"></use></svg><div class="grow"><div class="strong truncate"></div><div class="suggestion-meta"></div></div>';
          link.querySelector(".strong").textContent = item.verb + (item.name ? " · " + item.name : "");
          link.querySelector(".suggestion-meta").textContent = item.when;
          panel.appendChild(link);
        });
        loaded = true;
      } catch (e) {
        panel.innerHTML = '<div class="suggestion-empty">Could not load notifications.</div>';
      }
    });
  })();

  /* ------------------------------------------------------------ ajax forms */
  doc.addEventListener("submit", async function (event) {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-ajax]")) return;
    event.preventDefault();

    if (form.dataset.confirm) {
      const okay = await confirmDialog({
        title: form.dataset.confirmTitle || "Are you sure?",
        body: form.dataset.confirm,
        confirmLabel: form.dataset.confirmLabel || "Continue",
        note: form.dataset.confirmNote || "",
        danger: form.dataset.confirmDanger !== "false",
      });
      if (!okay) return;
    }

    const submit = form.querySelector('[type="submit"]');
    if (submit && submit.disabled) return;
    if (submit) submit.disabled = true;
    try {
      const data = await api(form.action || window.location.href, { form: new FormData(form) });
      if (data && data.url) {
        window.dispatchEvent(new CustomEvent("doxari:link", { detail: data.url }));
      }
      toast(data.message || "Done.");
      if (form.dataset.after === "reload") {
        setTimeout(() => window.location.reload(), 420);
      } else if (form.dataset.close) {
        const dialog = form.closest("dialog");
        if (dialog) dialog.close();
      }
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (submit) submit.disabled = false;
    }
  });

  // Favorite buttons work standalone too.
  doc.addEventListener("click", async function (event) {
    const button = event.target.closest("[data-favorite]");
    if (!button) return;
    event.preventDefault();
    try {
      const data = await api("/documents/" + button.dataset.favorite + "/favorite", { method: "POST" });
      button.classList.toggle("is-favorite", data.favorite);
      const use = button.querySelector("use");
      if (use) use.setAttribute("href", "/static/img/icons.svg#i-star" + (data.favorite ? "-filled" : ""));
      button.setAttribute("aria-pressed", String(data.favorite));
      toast(data.message);
    } catch (error) {
      toast(error.message, "error");
    }
  });

  /* ------------------------------------------------------------ select / bulk */
  (function initBulk() {
    const checks = () => $$("[data-doc-check]");
    const bar = $("[data-bulk-bar]");
    if (!bar) return;
    const countEl = $("[data-bulk-count]", bar);

    const refresh = () => {
      const selected = checks().filter((c) => c.checked);
      if (countEl) countEl.textContent = selected.length + " selected";
      bar.classList.toggle("is-open", selected.length > 0);
      body.classList.toggle("bulk-mode", selected.length > 0);
      checks().forEach((check) => {
        const card = check.closest(".doc-card, .doc-row");
        if (card) card.classList.toggle("selected", check.checked);
      });
    };

    doc.addEventListener("change", (event) => {
      if (event.target.matches("[data-doc-check]")) refresh();
      if (event.target.matches("[data-select-all]")) {
        const on = event.target.checked;
        checks().forEach((c) => (c.checked = on));
        refresh();
      }
    });

    $$("[data-bulk-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const ids = checks().filter((c) => c.checked).map((c) => c.dataset.docCheck);
        if (!ids.length) return;
        const action = button.dataset.bulkAction;
        if (action === "purge" || action === "delete") {
          const okay = await confirmDialog({
            title: action === "purge" ? "Delete permanently?" : "Move to trash?",
            body:
              action === "purge"
                ? ids.length + " document(s) will be deleted permanently. This cannot be undone."
                : ids.length + " document(s) will move to trash. You can restore them later.",
            confirmLabel: action === "purge" ? "Delete permanently" : "Move to trash",
          });
          if (!okay) return;
        }
        button.disabled = true;
        try {
          const data = await api("/api/bulk", {
            json: {
              action: action,
              ids: ids,
              folder_id: ($("#bulkFolder") || {}).value || null,
            },
          });
          toast(data.message);
          setTimeout(() => window.location.reload(), 600);
        } catch (error) {
          toast(error.message, "error");
        } finally {
          button.disabled = false;
        }
      });
    });

    if ($("[data-select-all]")) refresh();
  })();

  /* ------------------------------------------------------------ view mode */
  (function initViewMode() {
    $$("[data-view-mode]").forEach((link) => {
      link.addEventListener("click", () => {
        localStorage.setItem("doxari-view", link.dataset.viewMode);
        try {
          api("/api/preferences", { json: { view_mode: link.dataset.viewMode } });
        } catch (e) {}
      });
    });
  })();

  /* ------------------------------------------------------------ copy buttons */
  doc.addEventListener("click", async function (event) {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    event.preventDefault();
    const value = button.dataset.copy;
    try {
      await navigator.clipboard.writeText(value);
      toast("Link copied to clipboard.");
    } catch (e) {
      window.prompt("Copy this link:", value);
    }
  });

  doc.addEventListener("doxari:link", function (event) {
    const field = $("#shareLinkValue");
    if (field) {
      field.value = event.detail;
      const row = $("#shareLinkRow");
      if (row) row.hidden = false;
      field.select();
    }
  });

  /* ------------------------------------------------------------ auto submit filters */
  $$("[data-autosubmit]").forEach((field) => {
    field.addEventListener("change", () => {
      const form = field.closest("form");
      if (form) form.submit();
    });
  });

  /* ------------------------------------------------------------ upload */
  (function initUpload() {
    const dialog = $("#uploadModal");
    if (!dialog) return;
    const input = $("#uploadInput", dialog);
    const zone = $("#uploadZone", dialog);
    const queue = $("#uploadQueue", dialog);
    const form = $("#uploadForm", dialog);
    const submit = $("#uploadSubmit", dialog);
    const progressWrap = $("#uploadProgress", dialog);
    const progressBar = $("#uploadProgressBar", dialog);
    const progressText = $("#uploadProgressText", dialog);
    let files = [];

    const renderQueue = () => {
      queue.innerHTML = "";
      files.forEach((file, i) => {
        const item = doc.createElement("div");
        item.className = "queue-item";
        item.innerHTML =
          '<svg class="icon icon-sm"><use href="/static/img/icons.svg#i-file"></use></svg>' +
          '<span class="name"></span><span class="state"></span>' +
          '<div class="mini-progress"><span></span></div>';
        item.querySelector(".name").textContent = file.name;
        item.querySelector(".state").textContent = formatSize(file.size);
        item.dataset.index = String(i);
        queue.appendChild(item);
      });
      submit.disabled = files.length === 0;
    };

    const formatSize = (bytes) => {
      if (!bytes) return "";
      const units = ["B", "KB", "MB", "GB"];
      let value = bytes;
      let unit = 0;
      while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
      }
      return value.toFixed(unit === 0 ? 0 : 1) + " " + units[unit];
    };

    const addFiles = (list) => {
      Array.from(list).forEach((file) => {
        const duplicate = files.some((f) => f.name === file.name && f.size === file.size);
        if (!duplicate) files.push(file);
      });
      renderQueue();
    };

    if (zone) {
      zone.addEventListener("click", () => input.click());
      zone.addEventListener("dragover", (event) => {
        event.preventDefault();
        zone.classList.add("is-drag");
      });
      zone.addEventListener("dragleave", () => zone.classList.remove("is-drag"));
      zone.addEventListener("drop", (event) => {
        event.preventDefault();
        zone.classList.remove("is-drag");
        addFiles(event.dataTransfer.files);
      });
    }
    input.addEventListener("change", () => addFiles(input.files));

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!files.length || submit.disabled) return;
      const data = new FormData();
      files.forEach((file) => data.append("files", file));
      ["folder_id", "tags", "description"].forEach((name) => {
        const field = form.querySelector('[name="' + name + '"]');
        if (field && field.value) data.append(name, field.value);
      });
      data.append("csrf_token", CSRF);
      submit.disabled = true;
      if (progressWrap) progressWrap.hidden = false;

      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/upload");
      xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
      xhr.upload.addEventListener("progress", (e) => {
        if (!e.lengthComputable) return;
        const percent = Math.round((e.loaded / e.total) * 100);
        if (progressBar) progressBar.style.width = percent + "%";
        if (progressText) progressText.textContent = "Uploading… " + percent + "%";
      });
      xhr.addEventListener("load", () => {
        let payload = {};
        try {
          payload = JSON.parse(xhr.responseText);
        } catch (e) {}
        if (xhr.status >= 200 && xhr.status < 300 && payload.uploaded && payload.uploaded.length) {
          if (progressText) progressText.textContent = "Finishing…";
          toast(payload.message || "Upload complete.");
          if (payload.failed && payload.failed.length) {
            toast(payload.failed[0].name + ": " + payload.failed[0].error, "error");
          }
          files = [];
          renderQueue();
          setTimeout(() => window.location.reload(), 700);
        } else {
          const message =
            (payload.failed && payload.failed[0] && payload.failed[0].error) ||
            payload.error ||
            "The upload did not finish. Please try again.";
          toast(message, "error");
          if (progressText) progressText.textContent = "Upload failed";
          submit.disabled = false;
          if (progressBar) progressBar.style.width = "0%";
        }
      });
      xhr.addEventListener("error", () => {
        toast("Network problem during upload. Check your connection and retry.", "error");
        submit.disabled = false;
        if (progressText) progressText.textContent = "Upload failed";
      });
      xhr.send(data);
    });

    // Drag files anywhere on a page with an upload target.
    let dragDepth = 0;
    if (body.dataset.uploadEnabled === "1") {
      window.addEventListener("dragenter", (event) => {
        if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes("Files")) return;
        dragDepth += 1;
        body.classList.add("dragging");
      });
      window.addEventListener("dragleave", () => {
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) body.classList.remove("dragging");
      });
      window.addEventListener("dragover", (event) => event.preventDefault());
      window.addEventListener("drop", (event) => {
        if (!event.dataTransfer || !event.dataTransfer.files.length) return;
        event.preventDefault();
        dragDepth = 0;
        body.classList.remove("dragging");
        const source = event.target.closest(".dropzone");
        if (source) return;
        dialog.showModal();
        addFiles(event.dataTransfer.files);
      });
    }
  })();

  /* ------------------------------------------------------------ AI panel */
  (function initAI() {
    const panel = $("[data-ai-panel]");
    if (!panel) return;
    const documentId = panel.dataset.aiPanel;
    const output = $("[data-ai-output]", panel);
    const buttons = $$("[data-ai-task]", panel);
    const askForm = $("[data-ai-ask]", panel);

    const setOutput = (text, isError) => {
      if (!output) return;
      output.textContent = text;
      output.style.color = isError ? "var(--danger)" : "";
    };

    const run = async (task, extra) => {
      buttons.forEach((b) => (b.disabled = true));
      setOutput("Working…");
      try {
        const data = await api("/api/ai/" + task, { json: Object.assign({ document_id: documentId }, extra || {}) });
        if (task === "keywords") {
          setOutput((data.tags || []).join(", ") || "No tags suggested.");
        } else {
          setOutput(data.result || "No result.");
        }
      } catch (error) {
        if (error.code === "consent_required") {
          setOutput(error.message, true);
        } else {
          setOutput(error.message, true);
        }
      } finally {
        buttons.forEach((b) => (b.disabled = false));
      }
    };

    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const task = button.dataset.aiTask;
        run(task, task === "summarize" ? { style: button.dataset.style || "short" } : {});
      });
    });
    if (askForm) {
      askForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const input = askForm.querySelector("input");
        if (!input.value.trim()) return;
        run("ask", { question: input.value.trim() });
      });
    }
  })();

  /* ------------------------------------------------------------ signature forms */
  $$('input[type="file"][data-max-mb]').forEach((input) => {
    input.addEventListener("change", () => {
      const max = parseFloat(input.dataset.maxMb) * 1024 * 1024;
      const tooBig = Array.from(input.files || []).find((file) => file.size > max);
      if (tooBig) {
        toast("“" + tooBig.name + "” is larger than the " + input.dataset.maxMb + " MB limit.", "error");
        input.value = "";
      }
    });
  });

  /* ------------------------------------------------------------ bulk move */
  (function initBulkMove() {
    const form = $("#bulkMoveForm");
    const dialog = $("#bulkMoveModal");
    if (!form || !dialog) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const ids = $$("[data-doc-check]").filter((c) => c.checked).map((c) => c.dataset.docCheck);
      if (!ids.length) {
        toast("Select at least one document first.", "error");
        return;
      }
      const submit = form.querySelector('[type="submit"]');
      if (submit) submit.disabled = true;
      try {
        const data = await api("/api/bulk", {
          json: { action: "move", ids: ids, folder_id: ($("#bulkFolder") || {}).value || null },
        });
        toast(data.message);
        dialog.close();
        setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        toast(error.message, "error");
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  })();

  /* --------------------------------------------------- folder modal variants */
  (function initFolderModal() {
    const dialog = $("#folderModal");
    if (!dialog) return;
    const form = dialog.querySelector("form");
    const nameField = $("#folderName", dialog);
    const parentField = $("#folderParent", dialog);
    const title = $("#folderTitle", dialog);
    const submit = form.querySelector('[type="submit"]');

    const reset = () => {
      form.setAttribute("action", "/folders/create");
      if (nameField) nameField.value = "";
      if (parentField) {
        parentField.value = "";
        parentField.closest(".field").hidden = false;
      }
      if (title) title.textContent = "New folder";
      if (submit) submit.textContent = "Create folder";
    };

    $$('[data-modal-open="folderModal"]').forEach((trigger) => {
      trigger.addEventListener("click", () => {
        reset();
        if (trigger.dataset.folderEdit) {
          form.setAttribute("action", "/folders/" + trigger.dataset.folderEdit + "/rename");
          if (nameField) nameField.value = trigger.dataset.folderName || "";
          if (parentField) parentField.closest(".field").hidden = true;
          if (title) title.textContent = "Rename folder";
          if (submit) submit.textContent = "Save name";
        } else if (trigger.dataset.folderParent) {
          if (parentField) parentField.value = trigger.dataset.folderParent;
          if (title) title.textContent = "New subfolder";
        }
      });
    });

    // Double duty: this dialog is also used to edit a document's details when
    // opened with a form action (kept for older triggers).
    dialog.addEventListener("close", reset);
  })();

  /* --------------------------------------------------------- text preview */
  (function initTextPreview() {
    const pre = $("[data-text-preview]");
    if (!pre) return;
    const url = pre.dataset.textPreview;
    api(url, { method: "GET" })
      .then((data) => {
        pre.textContent = (data && data.text) || "This file is empty.";
      })
      .catch((error) => {
        pre.textContent = error.message || "That file could not be read.";
      });
  })();

  window.Doxari = { toast, api, confirm: confirmDialog };
})();
