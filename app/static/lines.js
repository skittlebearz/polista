(function () {
  var selected = null;
  var svgNS = "http://www.w3.org/2000/svg";
  var qsa = function (selector) { return document.querySelectorAll(selector); };
  var qs = function (selector) { return document.querySelector(selector); };
  function portSelector(side, port) {
    var escaped = window.CSS && CSS.escape ? CSS.escape(port) : port.replace(/"/g, '\\"');
    return '[data-side="' + side + '"][data-port="' + escaped + '"]';
  }
  function point(el, svgRect, edge) {
    var rect = el.getBoundingClientRect();
    return [(edge === "right" ? rect.right : rect.left) - svgRect.left, rect.top + rect.height / 2 - svgRect.top];
  }
  function redraw() {
    var svg = document.getElementById("lines");
    if (!svg) return;
    var svgRect = svg.getBoundingClientRect();
    svg.setAttribute("viewBox", "0 0 " + svgRect.width + " " + svgRect.height);
    svg.replaceChildren();
    qsa("[data-side][data-port]").forEach(function (el) {
      el.classList.remove("connected", "selected", "connected-to-selected", "conflict-pending");
    });
    qsa('[data-side="ingress"][data-mapped-egress]').forEach(function (ingress) {
      var egressPort = ingress.dataset.mappedEgress;
      if (!egressPort) return;
      var egress = qs(portSelector("egress", egressPort));
      if (!egress) return;
      ingress.classList.add("connected");
      egress.classList.add("connected");
      var start = point(ingress, svgRect, "right");
      var end = point(egress, svgRect, "left");
      var line = document.createElementNS(svgNS, "line");
      line.setAttribute("x1", start[0]);
      line.setAttribute("y1", start[1]);
      line.setAttribute("x2", end[0]);
      line.setAttribute("y2", end[1]);
      line.classList.add("connection");
      if (selected === ingress.dataset.port) line.classList.add("connected-to-selected");
      svg.appendChild(line);
    });
    var ingress = selected && qs(portSelector("ingress", selected));
    if (ingress) {
      ingress.classList.add("selected");
      var mapped = ingress.dataset.mappedEgress;
      var egress = mapped && qs(portSelector("egress", mapped));
      if (egress) egress.classList.add("connected-to-selected");
    }
    var confirm = qs("#dialog .conflict-confirm");
    if (confirm) {
      ["ingress", "egress"].forEach(function (side) {
        var input = confirm.querySelector('input[name="' + side + '"]');
        var el = input && qs(portSelector(side, input.value));
        if (el) el.classList.add("conflict-pending");
      });
    }
  }
  window.portofinoRedraw = redraw;
  document.addEventListener("click", function (event) {
    var target = event.target.closest ? event.target : event.target.parentElement;
    var edit = target && target.closest(".label-edit");
    if (edit) {
      var editPort = edit.closest("[data-side][data-port]");
      var input = editPort && editPort.querySelector('input[name="label"]');
      if (editPort && input) {
        editPort.classList.add("editing");
        input.focus();
        input.select();
      }
      return;
    }
    if (!target || target.closest("input, button, form")) return;
    var port = target.closest("[data-side][data-port]");
    if (!port) return;
    if (port.dataset.side === "ingress") {
      selected = selected === port.dataset.port ? null : port.dataset.port;
      redraw();
      return;
    }
    if (port.dataset.side === "egress" && selected) {
      var ingress = qs(portSelector("ingress", selected));
      if (!ingress || !window.htmx) return;
      var ingressPort = ingress.dataset.port;
      var egressPort = port.dataset.port;
      var samePair = ingress.dataset.mappedEgress === egressPort;
      var path = samePair ? "/ui/mappings/delete" : "/ui/mappings";
      var values = { ingress: ingressPort, egress: egressPort };
      if (!samePair) values.force = "false";
      window.htmx.ajax("POST", path, {
        target: "#ports",
        swap: "outerHTML",
        values: values
      });
      selected = null;
      redraw();
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || !event.target.matches('input[name="label"]')) return;
    var port = event.target.closest("[data-side][data-port]");
    var label = port && port.querySelector(".label-text");
    if (!port || !label) return;
    event.preventDefault();
    event.target.value = label.classList.contains("unlabeled") ? "" : label.textContent;
    port.classList.remove("editing");
    event.target.blur();
  });
  document.addEventListener("DOMContentLoaded", redraw);
  document.addEventListener("htmx:afterSwap", redraw);
  window.addEventListener("resize", redraw);
})();
