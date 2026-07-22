(function () {
  const nameInput = document.querySelector("[data-brand-name]");
  const colorInput = document.querySelector("[data-brand-color]");
  const logoInput = document.querySelector("[data-logo-url]");
  const previewName = document.querySelector("[data-preview-name]");
  const previewLogo = document.querySelector("[data-preview-logo]");
  const swatches = Array.from(document.querySelectorAll("[data-color]"));

  const initialsFor = (name) => {
    const cleanName = name.trim() || "ClientFlow";
    return cleanName.slice(0, 2).toUpperCase();
  };

  const renderLogo = () => {
    if (!previewLogo || !logoInput || !nameInput) return;

    const logoUrl = logoInput.value.trim();
    const studioName = nameInput.value.trim();

    previewLogo.innerHTML = "";
    if (logoUrl) {
      const image = document.createElement("img");
      image.src = logoUrl;
      image.alt = "";
      image.onerror = () => {
        previewLogo.innerHTML = "";
        const fallback = document.createElement("span");
        fallback.textContent = initialsFor(studioName);
        previewLogo.appendChild(fallback);
      };
      previewLogo.appendChild(image);
      return;
    }

    const fallback = document.createElement("span");
    fallback.textContent = initialsFor(studioName);
    previewLogo.appendChild(fallback);
  };

  const syncPreview = () => {
    if (nameInput && previewName) {
      previewName.textContent = nameInput.value.trim() || "ClientFlow MVP";
    }

    if (colorInput) {
      document.documentElement.style.setProperty("--primary", colorInput.value);
      document.documentElement.style.setProperty("--primary-strong", colorInput.value);

      swatches.forEach((swatch) => {
        swatch.classList.toggle("is-active", swatch.dataset.color.toLowerCase() === colorInput.value.toLowerCase());
      });
    }

    renderLogo();
  };

  nameInput?.addEventListener("input", syncPreview);
  colorInput?.addEventListener("input", syncPreview);
  logoInput?.addEventListener("input", syncPreview);

  swatches.forEach((swatch) => {
    swatch.addEventListener("click", () => {
      if (!colorInput) return;
      colorInput.value = swatch.dataset.color;
      syncPreview();
    });
  });

  syncPreview();
})();
