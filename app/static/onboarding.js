(function () {
  const dataNode = document.getElementById("lumaire-onboarding-data");
  if (!dataNode) return;

  let config;
  try {
    config = JSON.parse(dataNode.textContent);
  } catch {
    return;
  }

  if (!config || !config.key || !Array.isArray(config.steps)) return;

  const resolveTarget = (step) => {
    const selector = window.innerWidth <= 680 && step.mobileTarget
      ? step.mobileTarget
      : step.target;
    if (!selector) return null;
    try {
      return document.querySelector(selector);
    } catch {
      return null;
    }
  };

  const steps = config.steps.filter((step) => {
    if (!step || !step.title || !step.target) return false;
    return Boolean(resolveTarget(step));
  });
  if (!steps.length) return;

  const storageKey = `lumaire:onboarding:${config.userId || "shared"}:${config.key}:v2`;
  try {
    if (localStorage.getItem(storageKey) === "complete") return;
  } catch {
    // The tour can still run when storage is unavailable.
  }

  const tour = document.createElement("div");
  tour.className = "onboarding-tour";
  tour.innerHTML = `
    <div class="onboarding-shade onboarding-shade-top"></div>
    <div class="onboarding-shade onboarding-shade-right"></div>
    <div class="onboarding-shade onboarding-shade-bottom"></div>
    <div class="onboarding-shade onboarding-shade-left"></div>
    <div class="onboarding-spotlight" aria-hidden="true"></div>
    <section class="onboarding-tooltip" role="dialog" aria-modal="false" aria-labelledby="onboarding-title" aria-describedby="onboarding-body">
      <div class="onboarding-tooltip-accent"></div>
      <div class="onboarding-tooltip-content">
        <div class="onboarding-topline">
          <p class="onboarding-label"></p>
          <div class="onboarding-topline-actions">
            <span class="onboarding-progress"></span>
            <button class="onboarding-close" type="button" aria-label="Close tour">&times;</button>
          </div>
        </div>
        <h2 class="onboarding-title" id="onboarding-title"></h2>
        <p class="onboarding-body" id="onboarding-body"></p>
        <div class="onboarding-dots" aria-hidden="true"></div>
      </div>
      <div class="onboarding-actions">
        <button class="onboarding-button onboarding-skip" type="button">Skip tour</button>
        <div class="onboarding-actions-group">
          <button class="onboarding-button onboarding-back" type="button">Back</button>
          <button class="onboarding-button onboarding-button-primary onboarding-next" type="button">Next</button>
        </div>
      </div>
    </section>
  `;

  const shades = {
    top: tour.querySelector(".onboarding-shade-top"),
    right: tour.querySelector(".onboarding-shade-right"),
    bottom: tour.querySelector(".onboarding-shade-bottom"),
    left: tour.querySelector(".onboarding-shade-left"),
  };
  const spotlight = tour.querySelector(".onboarding-spotlight");
  const tooltip = tour.querySelector(".onboarding-tooltip");
  const content = tour.querySelector(".onboarding-tooltip-content");
  const label = tour.querySelector(".onboarding-label");
  const progress = tour.querySelector(".onboarding-progress");
  const title = tour.querySelector(".onboarding-title");
  const body = tour.querySelector(".onboarding-body");
  const dots = tour.querySelector(".onboarding-dots");
  const closeButton = tour.querySelector(".onboarding-close");
  const skipButton = tour.querySelector(".onboarding-skip");
  const actionsGroup = tour.querySelector(".onboarding-actions-group");
  const backButton = tour.querySelector(".onboarding-back");
  const nextButton = tour.querySelector(".onboarding-next");
  let activeStep = 0;
  let currentTarget = null;
  let positionFrame = null;
  let removeTargetAction = null;

  label.textContent = config.label || "Quick tour";
  steps.forEach(() => {
    const dot = document.createElement("span");
    dot.className = "onboarding-dot";
    dots.appendChild(dot);
  });

  const setRect = (element, rect) => {
    element.style.top = `${Math.max(0, rect.top)}px`;
    element.style.left = `${Math.max(0, rect.left)}px`;
    element.style.width = `${Math.max(0, rect.width)}px`;
    element.style.height = `${Math.max(0, rect.height)}px`;
  };

  const positionTour = () => {
    if (!currentTarget || !document.body.contains(tour)) return;

    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const padding = viewportWidth <= 680 ? 7 : 10;
    const targetRect = currentTarget.getBoundingClientRect();
    const focusRect = {
      top: Math.max(8, targetRect.top - padding),
      left: Math.max(8, targetRect.left - padding),
      right: Math.min(viewportWidth - 8, targetRect.right + padding),
      bottom: Math.min(viewportHeight - 8, targetRect.bottom + padding),
    };
    focusRect.width = Math.max(0, focusRect.right - focusRect.left);
    focusRect.height = Math.max(0, focusRect.bottom - focusRect.top);

    setRect(shades.top, {
      top: 0,
      left: 0,
      width: viewportWidth,
      height: focusRect.top,
    });
    setRect(shades.bottom, {
      top: focusRect.bottom,
      left: 0,
      width: viewportWidth,
      height: viewportHeight - focusRect.bottom,
    });
    setRect(shades.left, {
      top: focusRect.top,
      left: 0,
      width: focusRect.left,
      height: focusRect.height,
    });
    setRect(shades.right, {
      top: focusRect.top,
      left: focusRect.right,
      width: viewportWidth - focusRect.right,
      height: focusRect.height,
    });
    setRect(spotlight, focusRect);

    if (viewportWidth <= 680) {
      tooltip.dataset.placement = "mobile";
      tooltip.style.left = "12px";
      tooltip.style.right = "12px";
      tooltip.style.top = "auto";
      tooltip.style.bottom = "12px";
      tooltip.style.width = "auto";
      return;
    }

    tooltip.style.right = "auto";
    tooltip.style.bottom = "auto";
    tooltip.style.width = "min(360px, calc(100vw - 32px))";
    const tooltipRect = tooltip.getBoundingClientRect();
    const gap = 20;
    const edge = 16;
    const spaces = {
      right: viewportWidth - focusRect.right,
      left: focusRect.left,
      bottom: viewportHeight - focusRect.bottom,
      top: focusRect.top,
    };
    const preferred = steps[activeStep].placement;
    const order = [preferred, "right", "left", "bottom", "top"].filter(
      (value, index, values) => value && values.indexOf(value) === index
    );
    const fits = {
      right: spaces.right >= tooltipRect.width + gap,
      left: spaces.left >= tooltipRect.width + gap,
      bottom: spaces.bottom >= tooltipRect.height + gap,
      top: spaces.top >= tooltipRect.height + gap,
    };
    const placement = order.find((candidate) => fits[candidate])
      || Object.keys(spaces).sort((a, b) => spaces[b] - spaces[a])[0];

    let left;
    let top;
    if (placement === "right") {
      left = focusRect.right + gap;
      top = focusRect.top + (focusRect.height - tooltipRect.height) / 2;
    } else if (placement === "left") {
      left = focusRect.left - tooltipRect.width - gap;
      top = focusRect.top + (focusRect.height - tooltipRect.height) / 2;
    } else if (placement === "bottom") {
      left = focusRect.left + (focusRect.width - tooltipRect.width) / 2;
      top = focusRect.bottom + gap;
    } else {
      left = focusRect.left + (focusRect.width - tooltipRect.width) / 2;
      top = focusRect.top - tooltipRect.height - gap;
    }

    tooltip.dataset.placement = placement;
    tooltip.style.left = `${Math.min(Math.max(edge, left), viewportWidth - tooltipRect.width - edge)}px`;
    tooltip.style.top = `${Math.min(Math.max(edge, top), viewportHeight - tooltipRect.height - edge)}px`;
  };

  const schedulePosition = () => {
    if (positionFrame) cancelAnimationFrame(positionFrame);
    positionFrame = requestAnimationFrame(positionTour);
  };

  const finish = () => {
    try {
      localStorage.setItem(storageKey, "complete");
    } catch {
      // Dismissing the current tour should still work without storage.
    }
    if (positionFrame) cancelAnimationFrame(positionFrame);
    removeTargetAction?.();
    window.removeEventListener("resize", schedulePosition);
    window.removeEventListener("scroll", schedulePosition, true);
    tour.remove();
    document.body.classList.remove("onboarding-open");
  };

  const render = () => {
    const step = steps[activeStep];
    const isLast = activeStep === steps.length - 1;
    removeTargetAction?.();
    removeTargetAction = null;
    currentTarget = resolveTarget(step);
    if (!currentTarget) {
      activeStep += 1;
      if (activeStep >= steps.length) finish();
      else render();
      return;
    }

    progress.textContent = `${activeStep + 1} / ${steps.length}`;
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

    if (step.advanceOnTarget) {
      const advanceFromTarget = () => {
        window.setTimeout(() => {
          if (activeStep === steps.length - 1) finish();
          else {
            activeStep += 1;
            render();
          }
        }, 0);
      };
      currentTarget.addEventListener("click", advanceFromTarget, { once: true });
      removeTargetAction = () => currentTarget?.removeEventListener("click", advanceFromTarget);
    }

    const rect = currentTarget.getBoundingClientRect();
    const mobile = window.innerWidth <= 680;
    const visibleTop = mobile ? 76 : 24;
    const visibleBottom = mobile ? window.innerHeight * 0.5 : window.innerHeight - 24;
    if (rect.top < visibleTop || rect.bottom > visibleBottom) {
      currentTarget.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: mobile ? "start" : "center",
      });
      window.setTimeout(schedulePosition, 320);
    }
    schedulePosition();
    nextButton.focus({ preventScroll: true });
  };

  closeButton.addEventListener("click", finish);
  skipButton.addEventListener("click", finish);
  backButton.addEventListener("click", () => {
    activeStep = Math.max(0, activeStep - 1);
    render();
  });
  nextButton.addEventListener("click", () => {
    if (activeStep === steps.length - 1) {
      finish();
      return;
    }
    activeStep += 1;
    render();
  });
  document.addEventListener("keydown", (event) => {
    if (!document.body.contains(tour)) return;
    if (event.key === "Escape") finish();
    if (event.key === "ArrowRight") nextButton.click();
    if (event.key === "ArrowLeft" && activeStep > 0) backButton.click();
  });
  window.addEventListener("resize", schedulePosition);
  window.addEventListener("scroll", schedulePosition, true);

  document.body.appendChild(tour);
  document.body.classList.add("onboarding-open");
  render();
})();
