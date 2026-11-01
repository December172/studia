// Before first paint, so night readers never see a paper-white flash.
try {
  const theme = JSON.parse(localStorage.getItem("studia-theme"));
  if (theme === "night" || (theme == null && matchMedia("(prefers-color-scheme: dark)").matches)) document.documentElement.dataset.theme = "night";
} catch { /* storage blocked: follow the light default */ }
