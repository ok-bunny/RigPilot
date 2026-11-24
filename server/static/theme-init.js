"use strict";

(() => {
  // This controller now has one fixed palette. Saved legacy preferences and
  // the operating system color scheme cannot change its first paint.
  document.documentElement.dataset.theme = "dark";
  document.documentElement.style.colorScheme = "dark";
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", "#22252a");
})();
