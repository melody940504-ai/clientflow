import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.180.0/build/three.module.min.js";

const stage = document.querySelector("[data-stage-parallax]");
const stageCanvas = stage?.querySelector("[data-stage-canvas]");

if (stage && stageCanvas) {
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10);
  const renderer = new THREE.WebGLRenderer({
    canvas: stageCanvas,
    alpha: true,
    antialias: true,
    powerPreference: "high-performance",
  });
  const geometry = new THREE.PlaneGeometry(2, 2, 72, 48);
  const pointer = new THREE.Vector2(0.5, 0.5);
  const pointerTarget = new THREE.Vector2(0.5, 0.5);
  const clock = new THREE.Clock();
  const loader = new THREE.TextureLoader();
  const textures = {};
  let animationFrame = null;
  let isVisible = true;

  const material = new THREE.ShaderMaterial({
    transparent: true,
    uniforms: {
      uTexture: { value: null },
      uTime: { value: 0 },
      uPointer: { value: pointer },
      uResolution: { value: new THREE.Vector2(1, 1) },
      uTextureSize: { value: new THREE.Vector2(16, 9) },
      uDark: { value: 1 },
    },
    vertexShader: `
      uniform float uTime;
      uniform vec2 uPointer;
      varying vec2 vUv;
      varying float vFold;

      void main() {
        vUv = uv;
        vec3 transformed = position;
        float broad = sin((uv.x * 4.1 + uv.y * 0.9) * 3.14159 + uTime * 0.22);
        float fine = sin((uv.x * 8.4 - uv.y * 2.1) * 3.14159 - uTime * 0.16);
        float pointerFold = (uPointer.x - 0.5) * (uv.y - 0.5)
          + (0.5 - uPointer.y) * (uv.x - 0.5);
        transformed.z += broad * 0.075 + fine * 0.024 + pointerFold * 0.12;
        transformed.x += sin(uv.y * 5.2 + uTime * 0.12) * 0.014;
        transformed.y += cos(uv.x * 4.8 - uTime * 0.1) * 0.009;
        vFold = broad * 0.5 + fine * 0.22;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(transformed, 1.0);
      }
    `,
    fragmentShader: `
      uniform sampler2D uTexture;
      uniform vec2 uPointer;
      uniform vec2 uResolution;
      uniform vec2 uTextureSize;
      uniform float uDark;
      varying vec2 vUv;
      varying float vFold;

      vec2 coverUv(vec2 uv) {
        float screenAspect = uResolution.x / uResolution.y;
        float textureAspect = uTextureSize.x / uTextureSize.y;
        vec2 scale = vec2(1.0);
        if (screenAspect > textureAspect) {
          scale.y = textureAspect / screenAspect;
        } else {
          scale.x = screenAspect / textureAspect;
        }
        return (uv - 0.5) * scale + 0.5;
      }

      void main() {
        vec4 color = texture2D(uTexture, coverUv(vUv));
        float light = smoothstep(0.48, 0.0, distance(vUv, uPointer));
        float foldLight = vFold * (uDark > 0.5 ? 0.028 : 0.015);
        color.rgb += light * (uDark > 0.5 ? 0.085 : 0.035) + foldLight;
        gl_FragColor = color;
      }
    `,
  });
  const mesh = new THREE.Mesh(geometry, material);
  camera.position.z = 3;
  scene.add(mesh);

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6));
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const applyTheme = () => {
    const isDark = document.documentElement.dataset.theme === "dark";
    const texture = isDark ? textures.dark : textures.light;
    material.uniforms.uDark.value = isDark ? 1 : 0;
    if (texture) {
      material.uniforms.uTexture.value = texture;
      material.uniforms.uTextureSize.value.set(texture.image.width, texture.image.height);
      stage.classList.add("has-stage-webgl");
    }
  };

  const loadTexture = (key, source) => {
    loader.load(source, (texture) => {
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.minFilter = THREE.LinearFilter;
      textures[key] = texture;
      applyTheme();
    });
  };

  const resize = () => {
    const rect = stage.getBoundingClientRect();
    const width = Math.max(1, rect.width);
    const height = Math.max(1, rect.height);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    const visibleHeight = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * camera.position.z;
    mesh.scale.set((visibleHeight * camera.aspect / 2) * 1.1, (visibleHeight / 2) * 1.1, 1);
    material.uniforms.uResolution.value.set(width, height);
  };

  const render = () => {
    animationFrame = null;
    if (!isVisible) return;
    pointer.lerp(pointerTarget, 0.055);
    material.uniforms.uTime.value = clock.getElapsedTime();
    renderer.render(scene, camera);
    animationFrame = window.requestAnimationFrame(render);
  };

  stage.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch") return;
    const rect = stage.getBoundingClientRect();
    pointerTarget.set(
      Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
      Math.min(1, Math.max(0, 1 - (event.clientY - rect.top) / rect.height)),
    );
  });
  stage.addEventListener("pointerleave", () => pointerTarget.set(0.5, 0.5));
  new IntersectionObserver(([entry]) => {
    isVisible = entry.isIntersecting;
    if (isVisible && !animationFrame) animationFrame = window.requestAnimationFrame(render);
  }, { rootMargin: "160px" }).observe(stage);
  new MutationObserver(applyTheme).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  window.addEventListener("resize", resize);
  loadTexture("dark", "/static/images/lumaire-stage.png");
  loadTexture("light", "/static/images/lumaire-stage-light.png");
  resize();
  render();
}

const workflow = document.querySelector("[data-workflow-motion]");
const workflowCanvas = workflow?.querySelector("[data-workflow-canvas]");

if (workflow && workflowCanvas) {
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 100);
  const renderer = new THREE.WebGLRenderer({
    canvas: workflowCanvas,
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
        positions.push(centeredX + xDrift, centeredY + yDrift, broadFold + fineFold + diagonalPull + edgeLift);
        if (row < rows && column < columns) {
          const start = row * (columns + 1) + column;
          const nextRow = start + columns + 1;
          indices.push(start, start + 1, nextRow, start + 1, nextRow + 1, nextRow);
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
      const color = shadow.clone().lerp(highlight, 0.12 + depth * (isDark ? 0.72 : 0.84));
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
      roughness: 0.58,
      metalness: 0,
      clearcoat: 0.08,
      clearcoatRoughness: 0.72,
      sheen: 0.68,
      sheenColor: new THREE.Color(0xd8d0ff),
      side: THREE.DoubleSide,
      transparent: true,
      opacity: index === 0 ? 0.9 : 0.66,
    });
    const ribbon = new THREE.Mesh(
      createCurtainGeometry(config.width, config.height, config.columns, config.rows, config.phase),
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
      ribbon.material.roughness = isDark ? 0.58 : 0.62;
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
  });
  workflow.addEventListener("pointerleave", () => pointerTarget.set(0, 0));
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
