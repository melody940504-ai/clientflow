import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.180.0/build/three.module.min.js";

const workflow = document.querySelector("[data-workflow-motion]");
const canvas = workflow?.querySelector("[data-workflow-canvas]");

if (workflow && canvas) {
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 100);
  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: true,
    powerPreference: "high-performance",
  });
  const ribbonGroup = new THREE.Group();
  const ribbons = [];
  const pointer = new THREE.Vector2();
  const pointerTarget = new THREE.Vector2();
  const clock = new THREE.Clock();
  let isVisible = true;
  let animationFrame = null;

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.8));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.12;

  camera.position.set(0, 0, 10.5);
  scene.add(ribbonGroup);

  const ambient = new THREE.AmbientLight(0xbdb4ff, 0.52);
  const keyLight = new THREE.PointLight(0xe8e3ff, 24, 24, 1.8);
  const edgeLight = new THREE.PointLight(0x7760f0, 18, 20, 2);
  keyLight.position.set(0, 2, 7);
  edgeLight.position.set(-5, -2, 4);
  scene.add(ambient, keyLight, edgeLight);

  const darkColors = [0x171029, 0x0b0816, 0x251747];
  const darkHighlights = [0x9c86ff, 0x6f58cf, 0xc0b2ff];
  const lightShadows = [0x7561c7, 0x6652b2, 0x9989dd];
  const lightHighlights = [0xf8f6ff, 0xeeeaff, 0xffffff];

  const createCurtainGeometry = (width, height, columns, rows, phase) => {
    const positions = [];
    const indices = [];

    for (let row = 0; row <= rows; row += 1) {
      const v = row / rows;
      for (let column = 0; column <= columns; column += 1) {
        const u = column / columns;
        const centeredX = (u - 0.5) * width;
        const centeredY = (v - 0.5) * height;
        const broadFold = Math.sin((u * 5.2 + v * 1.05) * Math.PI + phase) * 0.58;
        const fineFold = Math.sin((u * 10.2 - v * 3.2) * Math.PI + phase * 1.7) * 0.19;
        const diagonalPull = Math.sin((u + v * 0.78) * Math.PI * 3.4 + phase) * 0.24;
        const edgeLift = Math.pow(Math.abs(u - 0.5) * 2, 1.8) * 0.34;
        const xDrift = Math.sin(v * Math.PI * 1.7 + phase) * (0.14 + Math.abs(u - 0.5) * 0.34);
        const yDrift = Math.sin(u * Math.PI * 3.6 + v * 1.4 + phase) * 0.22;
        const z = broadFold + fineFold + diagonalPull + edgeLift;

        positions.push(centeredX + xDrift, centeredY + yDrift, z);

        if (row < rows && column < columns) {
          const start = row * (columns + 1) + column;
          const nextRow = start + columns + 1;
          indices.push(start, start + 1, nextRow);
          indices.push(start + 1, nextRow + 1, nextRow);
        }
      }
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    geometry.setIndex(indices);
    geometry.computeVertexNormals();
    return geometry;
  };

  const curtainConfigs = [
    { width: 14.6, height: 10.2, columns: 96, rows: 62, phase: 0.35, x: 0, y: 0, z: 0, ry: -0.06, rz: -0.2 },
    { width: 9.2, height: 11.6, columns: 68, rows: 58, phase: 1.7, x: -5.4, y: 0.35, z: -1.7, ry: 0.56, rz: 0.32 },
    { width: 8.8, height: 11.2, columns: 68, rows: 58, phase: 3.05, x: 5.5, y: -0.3, z: -1.55, ry: -0.58, rz: -0.34 },
  ];

  const applyCurtainColors = (ribbon, index, isDark) => {
    const positions = ribbon.geometry.getAttribute("position");
    const colors = new Float32Array(positions.count * 3);
    const shadow = new THREE.Color(isDark ? darkColors[index] : lightShadows[index]);
    const highlight = new THREE.Color(isDark ? darkHighlights[index] : lightHighlights[index]);

    for (let vertex = 0; vertex < positions.count; vertex += 1) {
      const depth = Math.min(1, Math.max(0, (positions.getZ(vertex) + 1.25) / 2.5));
      const variation = 0.12 + depth * (isDark ? 0.72 : 0.84);
      const color = shadow.clone().lerp(highlight, variation);
      colors[vertex * 3] = color.r;
      colors[vertex * 3 + 1] = color.g;
      colors[vertex * 3 + 2] = color.b;
    }

    ribbon.geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  };

  curtainConfigs.forEach((config, index) => {
    const material = new THREE.MeshPhysicalMaterial({
      color: 0xffffff,
      vertexColors: true,
      roughness: 0.38,
      metalness: 0.04,
      clearcoat: 0.42,
      clearcoatRoughness: 0.32,
      sheen: 0.9,
      sheenColor: new THREE.Color(0xd8d0ff),
      side: THREE.DoubleSide,
      transparent: true,
      opacity: index === 0 ? 0.9 : 0.66,
    });
    const ribbon = new THREE.Mesh(
      createCurtainGeometry(
        config.width,
        config.height,
        config.columns,
        config.rows,
        config.phase,
      ),
      material,
    );
    ribbon.position.set(config.x, config.y, config.z);
    ribbon.rotation.set(0, config.ry, config.rz);
    ribbon.userData.baseX = config.x;
    ribbon.userData.baseY = config.y;
    ribbon.userData.phase = config.phase;
    applyCurtainColors(ribbon, index, true);
    ribbonGroup.add(ribbon);
    ribbons.push(ribbon);
  });

  ribbonGroup.rotation.x = -0.08;

  const updateTheme = () => {
    const isDark = document.documentElement.dataset.theme === "dark";
    ribbons.forEach((ribbon, index) => {
      applyCurtainColors(ribbon, index, isDark);
      ribbon.material.roughness = isDark ? 0.36 : 0.5;
      ribbon.material.opacity = isDark
        ? (index === 0 ? 0.86 : 0.58)
        : (index === 0 ? 0.78 : 0.5);
      ribbon.material.sheenColor.setHex(isDark ? 0xd8d0ff : 0xffffff);
    });
    ambient.intensity = isDark ? 0.16 : 0.38;
    keyLight.intensity = isDark ? 30 : 25;
    edgeLight.intensity = isDark ? 20 : 13;
    renderer.toneMappingExposure = isDark ? 1.12 : 1.02;
  };

  const resize = () => {
    const rect = workflow.getBoundingClientRect();
    const width = Math.max(1, rect.width);
    const height = Math.max(1, window.innerHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  };

  const render = () => {
    animationFrame = null;
    if (!isVisible) return;

    const elapsed = clock.getElapsedTime();
    pointer.lerp(pointerTarget, 0.065);
    ribbonGroup.rotation.y += (pointer.x * 0.24 - ribbonGroup.rotation.y) * 0.055;
    ribbonGroup.rotation.x += (-pointer.y * 0.15 - 0.08 - ribbonGroup.rotation.x) * 0.055;
    ribbonGroup.position.x += (pointer.x * 0.34 - ribbonGroup.position.x) * 0.05;
    ribbonGroup.position.y += (-pointer.y * 0.18 - ribbonGroup.position.y) * 0.05;

    keyLight.position.x += (pointer.x * 6.8 - keyLight.position.x) * 0.08;
    keyLight.position.y += (-pointer.y * 4.4 + 1.2 - keyLight.position.y) * 0.08;
    edgeLight.position.x += (-pointer.x * 4.4 - 2.4 - edgeLight.position.x) * 0.05;

    ribbons.forEach((ribbon, index) => {
      ribbon.position.x = ribbon.userData.baseX
        + Math.cos(elapsed * 0.18 + ribbon.userData.phase) * (index === 0 ? 0.035 : 0.1);
      ribbon.position.y = ribbon.userData.baseY
        + Math.sin(elapsed * 0.24 + ribbon.userData.phase) * (index === 0 ? 0.04 : 0.1);
    });

    renderer.render(scene, camera);
    animationFrame = window.requestAnimationFrame(render);
  };

  const start = () => {
    if (!animationFrame && isVisible) animationFrame = window.requestAnimationFrame(render);
  };

  workflow.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch") return;
    const rect = workflow.getBoundingClientRect();
    pointerTarget.set(
      Math.min(1, Math.max(-1, ((event.clientX - rect.left) / rect.width - 0.5) * 2)),
      Math.min(1, Math.max(-1, ((event.clientY - rect.top) / window.innerHeight - 0.5) * 2)),
    );
    start();
  });

  workflow.addEventListener("pointerleave", () => {
    pointerTarget.set(0, 0);
    start();
  });

  new IntersectionObserver(([entry]) => {
    isVisible = entry.isIntersecting;
    if (isVisible) start();
  }, { rootMargin: "200px" }).observe(workflow);

  new MutationObserver(updateTheme).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });

  window.addEventListener("resize", resize);
  updateTheme();
  resize();
  workflow.classList.add("has-webgl-scene");
  start();
}
