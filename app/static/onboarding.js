(function () {
  const dataNode = document.getElementById("lumaire-onboarding-data");
  if (!dataNode) return;

  let config;
  try {
    config = JSON.parse(dataNode.textContent);
  } catch {
    return;
  }

  if (!config || !config.key || !Array.isArray(config.steps) || !config.steps.length) return;

  const storageKey = `lumaire:onboarding:${config.userId || "shared"}:${config.key}:v1`;
  try {
    if (localStorage.getItem(storageKey) === "complete") return;
  } catch {
    // The tour can still run when storage is unavailable.
  }

  const overlay = document.createElement("div");
  overlay.className = "onboarding-overlay";
  overlay.setAttribute("role", "presentation");
  overlay.innerHTML = `
    <div class="onboarding-backdrop"></div>
    <section class="onboarding-dialog" role="dialog" aria-modal="true" aria-labelledby="onboarding-title" aria-describedby="onboarding-body">
      <div class="onboarding-accent"></div>
      <div class="onboarding-content">
        <div class="onboarding-topline">
          <p class="onboarding-label"></p>
          <span class="onboarding-progress"></span>
        </div>
        <div class="onboarding-step-number"></div>
        <h2 class="onboarding-title" id="onboarding-title"></h2>
        <p class="onboarding-body" id="onboarding-body"></p>
        <div class="onboarding-dots" aria-hidden="true"></div>
      </div>
      <div class="onboarding-actions">
        <button class="onboarding-button onboarding-skip" type="button">Skip</button>
        <div class="onboarding-actions-group">
          <button class="onboarding-button onboarding-back" type="button">Back</button>
          <button class="onboarding-button onboarding-button-primary onboarding-next" type="button">Next</button>
        </div>
      </div>
    </section>
  `;

  const label = overlay.querySelector(".onboarding-label");
  const content = overlay.querySelector(".onboarding-content");
  const progress = overlay.querySelector(".onboarding-progress");
  const stepNumber = overlay.querySelector(".onboarding-step-number");
  const title = overlay.querySelector(".onboarding-title");
  const body = overlay.querySelector(".onboarding-body");
  const dots = overlay.querySelector(".onboarding-dots");
  const skipButton = overlay.querySelector(".onboarding-skip");
  const actionsGroup = overlay.querySelector(".onboarding-actions-group");
  const backButton = overlay.querySelector(".onboarding-back");
  const nextButton = overlay.querySelector(".onboarding-next");
  let activeStep = 0;

  label.textContent = config.label || "Quick tour";
  config.steps.forEach(() => {
    const dot = document.createElement("span");
    dot.className = "onboarding-dot";
    dots.appendChild(dot);
  });

  const finish = () => {
    try {
      localStorage.setItem(storageKey, "complete");
    } catch {
      // Dismissing the current tour should still work without storage.
    }
    overlay.remove();
    document.body.classList.remove("onboarding-open");
  };

  const render = () => {
    const step = config.steps[activeStep];
    const isLast = activeStep === config.steps.length - 1;
    progress.textContent = `${activeStep + 1} / ${config.steps.length}`;
    stepNumber.textContent = String(activeStep + 1).padStart(2, "0");
    title.textContent = step.title;
    body.textContent = step.body;
    backButton.hidden = activeStep === 0;
    actionsGroup.classList.toggle("is-first-step", activeStep === 0);
    nextButton.textContent = isLast ? "Done" : "Next";
    Array.from(dots.children).forEach((dot, index) => {
      dot.classList.toggle("is-active", index === activeStep);
    });
    content.classList.remove("is-changing");
    void content.offsetWidth;
    content.classList.add("is-changing");
    nextButton.focus();
  };

  skipButton.addEventListener("click", finish);
  backButton.addEventListener("click", () => {
    activeStep = Math.max(0, activeStep - 1);
    render();
  });
  nextButton.addEventListener("click", () => {
    if (activeStep === config.steps.length - 1) {
      finish();
      return;
    }
    activeStep += 1;
    render();
  });
  document.addEventListener("keydown", (event) => {
    if (!document.body.contains(overlay)) return;
    if (event.key === "Escape") finish();
    if (event.key === "ArrowRight") nextButton.click();
    if (event.key === "ArrowLeft" && activeStep > 0) backButton.click();
  });

  document.body.appendChild(overlay);
  document.body.classList.add("onboarding-open");
  render();
})();
