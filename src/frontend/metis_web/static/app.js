// Small progressive enhancements. Every page works without JavaScript.
(function () {
  "use strict";

  function onReady(root) {
    root.querySelectorAll("[data-copy]").forEach(function (button) {
      if (button.dataset.bound) return;
      button.dataset.bound = "1";
      button.addEventListener("click", function () {
        var target = document.getElementById(button.dataset.copy);
        if (!target) return;
        target.select();
        var done = function () {
          var label = button.textContent;
          button.textContent = "Copied";
          setTimeout(function () { button.textContent = label; }, 1600);
        };
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(target.value).then(done, function () {});
        } else {
          done();
        }
      });
    });

    root.querySelectorAll("[data-fill]").forEach(function (button) {
      if (button.dataset.bound) return;
      button.dataset.bound = "1";
      button.addEventListener("click", function () {
        var target = document.getElementById(button.dataset.target || "message");
        if (!target) return;
        target.value = button.dataset.fill;
        target.focus();
      });
    });

    root.querySelectorAll("textarea[data-submit-on-enter]").forEach(function (area) {
      if (area.dataset.bound) return;
      area.dataset.bound = "1";
      area.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          if (area.value.trim() && area.form) area.form.requestSubmit();
        }
      });
    });

    root.querySelectorAll("input[type=file][data-max-files]").forEach(function (input) {
      if (input.dataset.bound) return;
      input.dataset.bound = "1";
      input.addEventListener("change", function () {
        var max = parseInt(input.dataset.maxFiles, 10);
        var status = document.getElementById(input.id + "-status");
        if (input.files.length > max) {
          input.setCustomValidity("Choose at most " + max + " files at a time.");
        } else {
          input.setCustomValidity("");
        }
        if (status) {
          status.textContent = input.files.length
            ? input.files.length + " file" + (input.files.length === 1 ? "" : "s") + " selected"
            : "";
        }
        input.reportValidity();
      });
    });

    var latest = root.querySelector("#latest");
    if (latest && window.location.hash === "#latest") {
      latest.scrollIntoView({ block: "start" });
    }
    var toggle = document.getElementById("nav-toggle");
    if (toggle) toggle.checked = false;
  }

  document.addEventListener("DOMContentLoaded", function () { onReady(document); });
  document.addEventListener("htmx:load", function (event) { onReady(event.detail.elt); });
  document.addEventListener("click", function (event) {
    document.querySelectorAll("details.org-switcher[open]").forEach(function (menu) {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });
})();
