// Map DOM selections in the proofreading body back to Unicode code point
// offsets (0-based, half-open) so the server never has to guess what the
// browser selected.

(function () {
  "use strict";

  const body = document.getElementById("body");
  if (!body) return;

  const startInput = document.getElementById("start");
  const endInput = document.getElementById("end");
  const replacement = document.getElementById("replacement");
  const label = document.getElementById("selection-label");
  const lookupButton = document.getElementById("lookup-selection");
  const clearButton = document.getElementById("clear-selection");
  const lookupStart = document.querySelector('input[name="lookup_start"]');
  const lookupEnd = document.querySelector('input[name="lookup_end"]');
  const reviseForm = document.getElementById("revise-form");

  const codePointLength = (s) => Array.from(s).length;

  // Code point offset of a (node, offset) boundary from the start of #body.
  // Building a DOM Range and measuring its text content is robust for both
  // text-node offsets and element-container offsets, and code point length
  // (not UTF-16 units) is what the server stores.
  function boundaryToCodePoint(node, offset) {
    const range = document.createRange();
    range.setStart(body, 0);
    range.setEnd(node, offset);
    return codePointLength(range.toString());
  }

  function selectionCodePoints() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    const range = sel.getRangeAt(0);
    if (!body.contains(range.commonAncestorContainer)) return null;
    const start = boundaryToCodePoint(range.startContainer, range.startOffset);
    const end = boundaryToCodePoint(range.endContainer, range.endOffset);
    return start < end ? { start, end } : null;
  }

  function setSelectionFields(sel) {
    if (!sel) {
      if (label) label.textContent = "未选择";
      startInput && (startInput.value = "");
      endInput && (endInput.value = "");
      return;
    }
    if (label) {
      label.textContent = `[${sel.start}, ${sel.end}) · ${sel.end - sel.start} 个码点`;
    }
    if (startInput) startInput.value = sel.start;
    if (endInput) endInput.value = sel.end;
    if (replacement) replacement.focus();
  }

  function refresh() {
    const sel = selectionCodePoints();
    setSelectionFields(sel);
    window.__ocrSelection = sel;
  }

  document.addEventListener("selectionchange", function () {
    // selectionchange fires for the whole document; only react when the
    // anchor lives inside the body pane.
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    if (!body.contains(sel.anchorNode) && !body.contains(sel.focusNode)) return;
    refresh();
  });

  if (clearButton) {
    clearButton.addEventListener("click", function () {
      window.getSelection().removeAllRanges();
      setSelectionFields(null);
    });
  }

  if (lookupButton) {
    lookupButton.addEventListener("click", function () {
      const sel = window.__ocrSelection || selectionCodePoints();
      if (!sel) {
        alert("请先在正文中选中一段非空文字。");
        return;
      }
      if (lookupStart) lookupStart.value = sel.start;
      if (lookupEnd) lookupEnd.value = sel.end;
      lookupButton.form.submit();
    });
  }

  if (reviseForm) {
    reviseForm.addEventListener("submit", function (event) {
      const start = Number(startInput && startInput.value);
      const end = Number(endInput && endInput.value);
      if (!Number.isInteger(start) || !Number.isInteger(end) || start >= end) {
        event.preventDefault();
        alert("请选择一段非空正文：起点和终点必须是整数，且起点小于终点。");
      }
    });
  }

  body.setAttribute("tabindex", "0");

  // Double-click a provenance span to select it wholesale.
  body.addEventListener("dblclick", function () {
    setTimeout(refresh, 0);
  });
})();
