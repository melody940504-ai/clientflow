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
        float foldLight = vFold * (uDark > 0.5 ? 0.028 : 0.015);
        color.rgb += foldLight;
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
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 10);
  const renderer = new THREE.WebGLRenderer({
    canvas: workflowCanvas,
    alpha: true,
    antialias: true,
    powerPreference: "high-performance",
  });
  const geometry = new THREE.PlaneGeometry(2, 2, 96, 64);
  const pointer = new THREE.Vector2(0.5, 0.5);
  const pointerTarget = new THREE.Vector2(0.5, 0.5);
  const clock = new THREE.Clock();
  let isVisible = true;
  let animationFrame = null;

  const material = new THREE.ShaderMaterial({
    transparent: false,
    uniforms: {
      uTime: { value: 0 },
      uPointer: { value: pointer },
      uResolution: { value: new THREE.Vector2(1, 1) },
      uDark: { value: 1 },
    },
    vertexShader: `
      uniform float uTime;
      uniform vec2 uPointer;
      varying vec2 vUv;
      varying float vDepth;

      void main() {
        vUv = uv;
        vec3 transformed = position;
        vec2 centered = uv - 0.5;
        float diagonal = centered.x * 0.82 + centered.y * 0.58;
        float crossFold = centered.x * 0.48 - centered.y * 0.86;
        float broad = sin(diagonal * 17.0 + sin(centered.y * 4.2) * 0.72 + uTime * 0.34) * 0.11;
        float soft = sin(crossFold * 9.0 - uTime * 0.27) * 0.052;
        float drift = sin((diagonal + centered.y * 0.22) * 6.0 + uTime * 0.2) * 0.032;
        float pointerPull = (uPointer.x - 0.5) * (uv.y - 0.5)
          + (0.5 - uPointer.y) * (uv.x - 0.5);
        vDepth = broad + soft + drift;
        transformed.z += vDepth + pointerPull * 0.09;
        transformed.x += sin(uv.y * 4.4 + uTime * 0.2) * 0.014;
        transformed.y += cos(uv.x * 4.1 - uTime * 0.17) * 0.011;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(transformed, 1.0);
      }
    `,
    fragmentShader: `
      uniform float uTime;
      uniform vec2 uPointer;
      uniform vec2 uResolution;
      uniform float uDark;
      varying vec2 vUv;
      varying float vDepth;

      float random(vec2 point) {
        return fract(sin(dot(point, vec2(12.9898, 78.233))) * 43758.5453);
      }

      void main() {
        vec2 centered = vUv - 0.5;
        float diagonal = centered.x * 0.82 + centered.y * 0.58;
        float crossFold = centered.x * 0.48 - centered.y * 0.86;
        float foldA = sin(diagonal * 17.0 + sin(centered.y * 4.2) * 0.72 + uTime * 0.34);
        float foldB = sin(crossFold * 9.0 - uTime * 0.27);
        float foldC = sin((diagonal + centered.y * 0.22) * 6.0 + uTime * 0.2);
        float fold = foldA * 0.64 + foldB * 0.25 + foldC * 0.11;
        float ridge = pow(max(0.0, fold), 3.6);
        float valley = pow(max(0.0, -fold), 2.0);
        float satinSheen = pow(max(0.0, sin(diagonal * 17.0 + uTime * 0.34 + 0.55)), 10.0);

        vec3 darkShadow = vec3(0.010, 0.010, 0.013);
        vec3 darkBase = vec3(0.052, 0.051, 0.056);
        vec3 darkHighlight = vec3(0.48, 0.47, 0.50);
        vec3 lightShadow = vec3(0.50, 0.48, 0.58);
        vec3 lightBase = vec3(0.76, 0.74, 0.82);
        vec3 lightHighlight = vec3(0.985, 0.98, 1.0);

        vec3 shadowColor = mix(lightShadow, darkShadow, uDark);
        vec3 baseColor = mix(lightBase, darkBase, uDark);
        vec3 highlightColor = mix(lightHighlight, darkHighlight, uDark);
        vec3 color = mix(shadowColor, baseColor, smoothstep(-0.82, 0.3, fold));
        color = mix(color, highlightColor, ridge * (uDark > 0.5 ? 0.56 : 0.68));
        color = mix(color, highlightColor, satinSheen * (uDark > 0.5 ? 0.22 : 0.18));
        color *= 1.0 - valley * (uDark > 0.5 ? 0.42 : 0.18);

        float grain = (random(gl_FragCoord.xy + floor(uTime * 2.0)) - 0.5)
          * (uDark > 0.5 ? 0.006 : 0.004);
        color += grain;

        vec2 correctedPointer = vec2(uPointer.x, 1.0 - uPointer.y);
        float pointerLight = smoothstep(0.42, 0.0, distance(vUv, correctedPointer));
        color += pointerLight * (uDark > 0.5 ? 0.12 : 0.075);

        float edgeShade = smoothstep(0.78, 0.18, distance(vUv, vec2(0.5)));
        color *= mix(uDark > 0.5 ? 0.72 : 0.9, 1.0, edgeShade);
        gl_FragColor = vec4(color, 1.0);
      }
    `,
  });
  const cloth = new THREE.Mesh(geometry, material);
  camera.position.z = 3;
  scene.add(cloth);

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6));
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const updateTheme = () => {
    const isDark = document.documentElement.dataset.theme === "dark";
    material.uniforms.uDark.value = isDark ? 1 : 0;
  };

  const resize = () => {
    const rect = workflow.getBoundingClientRect();
    const width = Math.max(1, rect.width);
    const height = Math.max(1, window.innerHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    const visibleHeight = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * camera.position.z;
    cloth.scale.set((visibleHeight * camera.aspect / 2) * 1.1, (visibleHeight / 2) * 1.1, 1);
    material.uniforms.uResolution.value.set(width, height);
  };

  const render = () => {
    animationFrame = null;
    if (!isVisible) return;
    pointer.lerp(pointerTarget, 0.06);
    material.uniforms.uTime.value = clock.getElapsedTime();
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
      Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
      Math.min(1, Math.max(0, (event.clientY - rect.top) / window.innerHeight)),
    );
  });
  workflow.addEventListener("pointerleave", () => pointerTarget.set(0.5, 0.5));
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
