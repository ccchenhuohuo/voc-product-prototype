(() => {
  const interactiveSelector = "a,button,input,textarea,select,label,form,summary,[contenteditable='true'],[data-row-no-nav]";

  function storedTheme() {
    try {
      const value = localStorage.getItem("voc-theme");
      return ["light", "dark", "system"].includes(value) ? value : "system";
    } catch (_) {
      return "system";
    }
  }

  function applyTheme(theme, { persist = false } = {}) {
    const value = ["light", "dark", "system"].includes(theme) ? theme : "system";
    if (value === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", value);
    if (persist) {
      try { localStorage.setItem("voc-theme", value); } catch (_) { /* read-only storage */ }
    }
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.themeChoice === value));
    });
  }

  applyTheme(storedTheme());

  const searchInput = document.querySelector("[data-instant-search]");
  if (searchInput) {
    let searchTimer;
    let composing = false;
    const submitSearch = () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => searchInput.form?.requestSubmit(), 180);
    };
    searchInput.addEventListener("compositionstart", () => { composing = true; });
    searchInput.addEventListener("compositionend", () => {
      composing = false;
      submitSearch();
    });
    searchInput.addEventListener("input", () => {
      if (!composing) submitSearch();
    });
    if (new URLSearchParams(window.location.search).has("q")) {
      searchInput.focus();
      searchInput.setSelectionRange(searchInput.value.length, searchInput.value.length);
    }
  }

  function selectableRows() {
    return [...document.querySelectorAll("tbody tr[tabindex='0']")].filter(
      (row) => !row.hidden && row.getClientRects().length > 0,
    );
  }

  function selectRow(row, { focus = false } = {}) {
    if (!row) return;
    const rows = selectableRows();
    rows.forEach((candidate) => candidate.classList.toggle("sel", candidate === row));
    if (focus) row.focus({ preventScroll: true });
    row.scrollIntoView({ block: "nearest" });
  }

  function currentRow(rows) {
    const focused = document.activeElement?.closest?.("tbody tr[tabindex='0']");
    if (focused && rows.includes(focused)) return focused;
    const selected = document.querySelector("tbody tr.sel");
    return (selected && rows.includes(selected) ? selected : rows[0]);
  }

  function closeStatusPanels(control) {
    let closed = false;
    control?.querySelectorAll("[data-status-panel]").forEach((panel) => {
      if (!panel.hidden) closed = true;
      panel.hidden = true;
    });
    control?.querySelectorAll("[data-status-toggle]").forEach((button) => {
      button.setAttribute("aria-expanded", "false");
    });
    return closed;
  }

  document.addEventListener("click", (event) => {
    const themeChoice = event.target.closest("[data-theme-choice]");
    if (themeChoice) {
      applyTheme(themeChoice.dataset.themeChoice, { persist: true });
      return;
    }

    const userToggle = event.target.closest("[data-user-toggle]");
    const userPopover = document.querySelector("[data-user-popover]");
    if (userToggle) {
      const shouldOpen = userPopover?.hidden ?? false;
      if (userPopover) userPopover.hidden = !shouldOpen;
      userToggle.setAttribute("aria-expanded", String(shouldOpen));
      return;
    }
    if (userPopover && !userPopover.hidden && !event.target.closest(".user-area")) {
      userPopover.hidden = true;
      document.querySelector("[data-user-toggle]")?.setAttribute("aria-expanded", "false");
    }

    const tagToggle = event.target.closest("[data-tag-toggle]");
    if (tagToggle) {
      const level = tagToggle.closest(".tag-level");
      const expanded = !level?.classList.contains("expanded");
      level?.classList.toggle("expanded", expanded);
      tagToggle.setAttribute("aria-expanded", String(expanded));
      return;
    }

    const toggle = event.target.closest("[data-status-toggle]");
    if (toggle) {
      const control = toggle.closest(".status-control");
      const wanted = toggle.dataset.statusToggle;
      const panel = control?.querySelector(`[data-status-panel="${wanted}"]`);
      const shouldOpen = Boolean(panel?.hidden);
      closeStatusPanels(control);
      if (panel && shouldOpen) {
        panel.hidden = false;
        toggle.setAttribute("aria-expanded", "true");
        panel.querySelector("textarea,input")?.focus();
      }
      return;
    }

    const cancel = event.target.closest("[data-status-cancel]");
    if (cancel) {
      closeStatusPanels(cancel.closest(".status-control"));
      return;
    }

    const row = event.target.closest("tbody tr[data-href]");
    if (!row || event.target.closest(interactiveSelector)) return;
    window.location.assign(row.dataset.href);
  });

  document.addEventListener("focusin", (event) => {
    const row = event.target.closest?.("tbody tr[tabindex='0']");
    if (row) selectRow(row);
  });

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target.closest?.("input,textarea,[contenteditable='true']")) return;

    const key = event.key.toLowerCase();
    const rows = selectableRows();
    if (key === "j" || key === "k") {
      if (!rows.length) return;
      event.preventDefault();
      const selected = currentRow(rows);
      const currentIndex = Math.max(0, rows.indexOf(selected));
      const delta = key === "j" ? 1 : -1;
      const nextIndex = Math.max(0, Math.min(rows.length - 1, currentIndex + delta));
      selectRow(rows[nextIndex], { focus: true });
      return;
    }

    if (event.key === "Enter") {
      if (event.target.closest?.(interactiveSelector)) return;
      const selected = currentRow(rows);
      if (selected?.dataset.href) {
        event.preventDefault();
        window.location.assign(selected.dataset.href);
      }
      return;
    }

    if (event.key === "Escape") {
      const userPopover = document.querySelector("[data-user-popover]:not([hidden])");
      if (userPopover) {
        event.preventDefault();
        userPopover.hidden = true;
        document.querySelector("[data-user-toggle]")?.setAttribute("aria-expanded", "false");
        document.querySelector("[data-user-toggle]")?.focus();
        return;
      }
      const openPanel = document.querySelector(".status-control [data-status-panel]:not([hidden])");
      if (openPanel) {
        event.preventDefault();
        closeStatusPanels(openPanel.closest(".status-control"));
        return;
      }
      const backUrl = document.body.dataset.backUrl;
      if (backUrl) {
        event.preventDefault();
        window.location.assign(backUrl);
      }
    }
  });

  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.xhr?.status === 422) event.detail.shouldSwap = true;
  });

})();
