// ==UserScript==
// @name         Claude Output auto-paste (__NAME__)
// @namespace    claude-code-output
// @version      1.0
// @description  When you click "__NAME__ ↗" in Claude Output, paste that query into this editor.
// @match        __MATCH__
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==
// Served by Claude Output at /claude-output-autopaste.user.js; the @match line is filled in from --console-url.
// On load it asks the local Claude Output server for a query handed over in the last minute (once only),
// waits for the editor to appear, and puts the query in it. Opening the editor yourself does nothing.
(function () {
  "use strict";
  const W = typeof unsafeWindow !== "undefined" ? unsafeWindow : window;

  function toast(msg, bad) {
    const t = document.createElement("div");
    t.textContent = msg;
    t.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:2147483647;padding:10px 16px;" +
      "border-radius:8px;font:13px -apple-system,sans-serif;color:#fff;background:" + (bad ? "#b91c1c" : "#1f2328") +
      ";box-shadow:0 4px 16px rgba(0,0,0,.2)";
    document.body.appendChild(t);
    setTimeout(() => t.remove(), bad ? 9000 : 4000);
  }

  // Put text in whatever editor the page uses. Known editor APIs first; then a paste event, which every
  // code editor handles the same way as a real Cmd+V; then a plain textarea.
  function insert(text) {
    // Monaco: the global most apps get from Monaco's loader (incl. @monaco-editor/react), or the loader module
    let ns = W.monaco;
    if (!ns && W.require && W.require.defined && W.require.defined("vs/editor/editor.main")) ns = W.require("vs/editor/editor.main");
    const monaco = ns && ns.editor;
    if (monaco) {
      const eds = monaco.getEditors ? monaco.getEditors() : [];
      if (eds.length) { eds[0].setValue(text); eds[0].focus(); return "monaco"; }
      const models = monaco.getModels ? monaco.getModels() : [];
      if (models.length) { models[0].setValue(text); return "monaco"; }
    }
    const cm5 = document.querySelector(".CodeMirror");
    if (cm5 && cm5.CodeMirror) { cm5.CodeMirror.setValue(text); cm5.CodeMirror.focus(); return "codemirror"; }
    const aceEl = document.querySelector(".ace_editor");
    if (aceEl && W.ace) { const ed = W.ace.edit(aceEl); ed.setValue(text, -1); ed.focus(); return "ace"; }
    const cm6 = document.querySelector(".cm-content");
    const view = cm6 && (cm6.cmView && cm6.cmView.view);
    if (view) { view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } }); view.focus(); return "codemirror6"; }

    // Monaco without a global (bundled into the app): paste into its hidden input. A paste keeps the text
    // exactly as is; typing it (the fallback) would let Monaco auto-indent every line.
    const mta = document.querySelector(".monaco-editor textarea");
    if (mta) {
      mta.focus();
      const lines = () => (mta.closest(".monaco-editor").querySelector(".view-lines") || {}).textContent || "";
      const before = lines();
      const dt = new DataTransfer();
      dt.setData("text/plain", text);
      mta.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
      if (lines() !== before) return "monaco-paste";
      document.execCommand("selectAll", false);
      // last resort: type it in (Monaco may re-indent lines; the original is still on the clipboard)
      if (document.execCommand("insertText", false, text)) return "monaco-typed";
    }

    const target = document.querySelector(".monaco-editor textarea, .cm-content, .ace_text-input, .CodeMirror textarea, " +
                                          "[contenteditable='true'], textarea");
    if (!target) return null;
    target.focus();
    if (target.tagName === "TEXTAREA" && !target.closest(".monaco-editor, .ace_editor, .CodeMirror")) {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(target, text);  // through the native setter, so React / Vue state sees the change
      target.dispatchEvent(new Event("input", { bubbles: true }));
      return "textarea";
    }
    const dt = new DataTransfer();
    dt.setData("text/plain", text);
    target.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    return "paste";
  }

  function waitAndInsert(text, path, deadline) {
    const how = insert(text);
    if (how) { toast(`Pasted ${path.split("/").pop()} from Claude Output`); return; }
    if (Date.now() < deadline) return setTimeout(() => waitAndInsert(text, path, deadline), 300);
    toast("Claude Output: couldn't find the editor on this page. The query is on your clipboard: press Cmd+V.", true);
  }

  GM_xmlhttpRequest({
    method: "GET",
    url: "http://127.0.0.1:__PORT__/api/handoff/take",
    headers: { "X-Claude-Output-Autopaste": "1" },
    onload: (r) => {
      let data = {};
      try { data = JSON.parse(r.responseText); } catch (e) { return; }
      if (data.sql) waitAndInsert(data.sql, data.path || "query", Date.now() + 20000);
    },
  });
})();
