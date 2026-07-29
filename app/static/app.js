(function () {
  const root = document.documentElement;
  const themeKey = 'theme';
  const legacyThemeKey = 'clientflow-theme';
  const stored = localStorage.getItem(themeKey) || localStorage.getItem(legacyThemeKey);

  if (stored) root.setAttribute('data-theme', stored);

  const saveTheme = (theme) => {
    root.setAttribute('data-theme', theme);
    localStorage.setItem(themeKey, theme);
    localStorage.setItem(legacyThemeKey, theme);
  };

  const themeButtons = Array.from(document.querySelectorAll('[data-theme-toggle], #themeToggle'));
  const updateThemeLabels = () => {
    const isDark = root.getAttribute('data-theme') === 'dark';
    themeButtons.forEach((button) => {
      if (button.hasAttribute('data-theme-icon')) {
        const label = isDark ? 'Switch to light mode' : 'Switch to dark mode';
        button.setAttribute('aria-label', label);
        button.setAttribute('title', label);
      } else {
        button.textContent = isDark ? 'Light mode' : 'Dark mode';
      }
    });
  };

  themeButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      saveTheme(next);
      updateThemeLabels();
    });
  });
  updateThemeLabels();

  const menuToggle = document.getElementById('site-menu-toggle');
  const menuPanel = document.getElementById('site-menu-panel');

  if (menuToggle && menuPanel) {
    menuToggle.addEventListener('click', (event) => {
      event.stopPropagation();
      const isHidden = menuPanel.classList.toggle('hidden');
      menuToggle.setAttribute('aria-expanded', String(!isHidden));
    });

    menuPanel.addEventListener('click', (event) => {
      event.stopPropagation();
    });

    document.addEventListener('click', () => {
      menuPanel.classList.add('hidden');
      menuToggle.setAttribute('aria-expanded', 'false');
    });
  }

  const parallaxStage = document.querySelector('[data-stage-parallax]');
  if (parallaxStage) {
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let parallaxFrame = null;
    let targetX = 0;
    let targetY = 0;

    const renderParallax = () => {
      parallaxFrame = null;
      parallaxStage.style.setProperty('--stage-bg-x', `${(targetX * 10).toFixed(2)}px`);
      parallaxStage.style.setProperty('--stage-bg-y', `${(targetY * 7).toFixed(2)}px`);
      parallaxStage.style.setProperty('--stage-copy-x', `${(targetX * 5).toFixed(2)}px`);
      parallaxStage.style.setProperty('--stage-copy-y', `${(targetY * 4).toFixed(2)}px`);
      parallaxStage.style.setProperty('--stage-window-x', `${(targetX * 22).toFixed(2)}px`);
      parallaxStage.style.setProperty('--stage-window-y', `${(targetY * 13).toFixed(2)}px`);
      parallaxStage.style.setProperty('--stage-rotate-x', `${(-targetY * 1.2).toFixed(2)}deg`);
      parallaxStage.style.setProperty('--stage-rotate-y', `${(targetX * 1.8).toFixed(2)}deg`);
      parallaxStage.style.setProperty('--stage-pin-x', `${(targetX * 9).toFixed(2)}px`);
      parallaxStage.style.setProperty('--stage-pin-y', `${(targetY * 6).toFixed(2)}px`);
    };

    const requestParallax = () => {
      if (parallaxFrame) return;
      parallaxFrame = window.requestAnimationFrame(renderParallax);
    };

    parallaxStage.addEventListener('pointermove', (event) => {
      if (reduceMotion || event.pointerType === 'touch') return;
      const rect = parallaxStage.getBoundingClientRect();
      targetX = Math.min(1, Math.max(-1, ((event.clientX - rect.left) / rect.width - 0.5) * 2));
      targetY = Math.min(1, Math.max(-1, ((event.clientY - rect.top) / rect.height - 0.5) * 2));
      requestParallax();
    });

    parallaxStage.addEventListener('pointerleave', () => {
      targetX = 0;
      targetY = 0;
      requestParallax();
    });

    renderParallax();
  }

  const showcase = document.querySelector('[data-product-showcase]');
  if (showcase) {
    const scenes = Array.from(showcase.querySelectorAll('[data-showcase-scene]'));
    const tabs = Array.from(showcase.querySelectorAll('[data-showcase-tab]'));
    const switcher = showcase.querySelector('.showcase-switcher');
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let activeIndex = 0;
    let rotationTimer = null;

    const restartProgress = () => {
      if (!switcher) return;
      switcher.classList.remove('is-progressing');
      void switcher.offsetWidth;
      switcher.classList.add('is-progressing');
    };

    const showScene = (nextIndex, userInitiated = false) => {
      activeIndex = (nextIndex + scenes.length) % scenes.length;

      scenes.forEach((scene, index) => {
        const isActive = index === activeIndex;
        scene.classList.toggle('is-active', isActive);
        scene.setAttribute('aria-hidden', String(!isActive));
      });

      tabs.forEach((tab, index) => {
        const isActive = index === activeIndex;
        tab.classList.toggle('is-active', isActive);
        tab.setAttribute('aria-selected', String(isActive));
        tab.tabIndex = isActive ? 0 : -1;
      });

      switcher?.style.setProperty('--showcase-index', String(activeIndex));
      restartProgress();

      if (userInitiated && window.innerWidth <= 640) {
        tabs[activeIndex]?.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'nearest', inline: 'center' });
      }
    };

    const stopRotation = () => {
      if (rotationTimer) window.clearInterval(rotationTimer);
      rotationTimer = null;
      showcase.classList.add('is-paused');
    };

    const startRotation = () => {
      stopRotation();
      showcase.classList.remove('is-paused');
      if (document.visibilityState === 'visible') {
        rotationTimer = window.setInterval(() => showScene(activeIndex + 1), 5200);
      }
    };

    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => {
        showScene(index, true);
        startRotation();
      });

      tab.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const nextIndex = event.key === 'Home'
          ? 0
          : event.key === 'End'
            ? tabs.length - 1
            : activeIndex + (event.key === 'ArrowRight' ? 1 : -1);
        showScene(nextIndex, true);
        tabs[activeIndex]?.focus();
        startRotation();
      });
    });

    showcase.addEventListener('focusin', stopRotation);
    showcase.addEventListener('focusout', startRotation);
    document.addEventListener('visibilitychange', startRotation);

    showScene(0);
    startRotation();
  }

  const workflowMotion = document.querySelector('[data-workflow-motion]');
  if (workflowMotion) {
    const stickyStage = workflowMotion.querySelector('.workflow-sticky');
    const ribbons = Array.from(workflowMotion.querySelectorAll('.workflow-ribbons i'));
    const steps = Array.from(workflowMotion.querySelectorAll('.workflow-steps li'));
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let workflowFrame = null;

    const updateWorkflow = () => {
      workflowFrame = null;
      const rect = workflowMotion.getBoundingClientRect();
      const scrollRange = Math.max(workflowMotion.offsetHeight - window.innerHeight, 1);
      const progress = reduceMotion
        ? 1
        : Math.min(1, Math.max(0, -rect.top / scrollRange));

      workflowMotion.style.setProperty('--workflow-progress', progress.toFixed(3));
      workflowMotion.style.setProperty('--workflow-scale', (0.92 + progress * 0.08).toFixed(3));
      workflowMotion.style.setProperty('--workflow-radius', `${Math.round(34 * (1 - progress))}px`);
      workflowMotion.classList.toggle('is-expanded', progress > 0.72);

      const ribbonOffsets = [-150, -54, 62, 148];
      ribbons.forEach((ribbon, index) => {
        ribbon.style.setProperty('--ribbon-shift', `${Math.round(ribbonOffsets[index] * (1 - progress))}px`);
      });

      steps.forEach((step, index) => {
        step.classList.toggle('is-active', progress >= 0.28 + index * 0.13);
      });
    };

    const requestWorkflowUpdate = () => {
      if (workflowFrame) return;
      workflowFrame = window.requestAnimationFrame(updateWorkflow);
    };

    workflowMotion.addEventListener('pointermove', (event) => {
      if (!stickyStage || reduceMotion) return;
      const rect = stickyStage.getBoundingClientRect();
      const x = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
      const y = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height));
      stickyStage.style.setProperty('--pointer-x', `${(x * 100).toFixed(1)}%`);
      stickyStage.style.setProperty('--pointer-y', `${(y * 100).toFixed(1)}%`);
      stickyStage.style.setProperty('--pointer-shift-x', `${((x - 0.5) * 18).toFixed(1)}px`);
      stickyStage.style.setProperty('--pointer-shift-y', `${((y - 0.5) * 12).toFixed(1)}px`);
    });

    workflowMotion.addEventListener('pointerleave', () => {
      stickyStage?.style.setProperty('--pointer-x', '50%');
      stickyStage?.style.setProperty('--pointer-y', '50%');
      stickyStage?.style.setProperty('--pointer-shift-x', '0px');
      stickyStage?.style.setProperty('--pointer-shift-y', '0px');
    });

    window.addEventListener('scroll', requestWorkflowUpdate, { passive: true });
    window.addEventListener('resize', requestWorkflowUpdate);
    updateWorkflow();
  }

  const reviewChecklist = document.querySelector('[data-review-checklist]');
  if (reviewChecklist) {
    const items = Array.from(reviewChecklist.querySelectorAll('[data-review-task]'));
    const status = reviewChecklist.querySelector('[data-review-status]');

    const updateReviewStatus = () => {
      const completed = items.filter((item) => item.classList.contains('is-complete')).length;
      if (status) status.textContent = `${completed} of ${items.length} resolved`;
    };

    items.forEach((item) => {
      item.addEventListener('click', () => {
        const isComplete = item.classList.toggle('is-complete');
        item.setAttribute('aria-pressed', String(isComplete));
        updateReviewStatus();
      });
    });

    updateReviewStatus();
  }

  document.querySelectorAll('[data-toolkit-notes] button').forEach((button) => {
    button.addEventListener('click', () => {
      const isResolved = button.getAttribute('aria-pressed') === 'true';
      button.setAttribute('aria-pressed', String(!isResolved));
    });
  });

  document.querySelectorAll('.toolkit-access').forEach((control) => {
    const buttons = Array.from(control.querySelectorAll('button'));
    buttons.forEach((button) => {
      button.addEventListener('click', () => {
        buttons.forEach((item) => item.classList.toggle('is-active', item === button));
      });
    });
  });
})();
