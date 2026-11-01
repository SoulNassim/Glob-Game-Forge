// 3D preview, thumbnails and sprite sheets for GLB / glTF / OBJ / FBX files.
// Loaded on demand from app.js; three.js comes from the import map in index.html.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";

const ANGLES = {
  front: { azimuth: 0, elevation: 0 },
  side: { azimuth: 90, elevation: 0 },
  iso: { azimuth: 45, elevation: 30 },
  top: { azimuth: 0, elevation: 65 },
};

// Models exported elsewhere often point at textures by an absolute path on the exporting machine
// (e.g. /home/app/mixamo/…/skin.fbm/basecolor.png). Anything outside the model's own folder is looked up
// by file name next to the model instead, where Glob keeps a model's textures.
function textureManager(url) {
  const base = new URL(url, location.href);
  const dir = base.href.slice(0, base.href.lastIndexOf("/") + 1);
  const manager = new THREE.LoadingManager();
  manager.setURLModifier((requested) => {
    if (requested.startsWith("data:") || requested.startsWith("blob:")) return requested;
    const resolved = new URL(requested.replace(/\\/g, "/"), dir).href;
    if (resolved.startsWith(dir)) return resolved;
    return dir + encodeURIComponent(decodeURIComponent(resolved.split("/").pop() || ""));
  });
  // Textures (embedded ones too) finish loading after the model itself; stills wait for them.
  let busy = false;
  const waiters = [];
  manager.onStart = () => { busy = true; };
  manager.onProgress = (_url, loaded, total) => { busy = loaded < total; };
  manager.onLoad = () => { busy = false; waiters.splice(0).forEach((done) => done()); };
  manager.settled = (ms = 15000) => (busy ? new Promise((done) => { waiters.push(done); setTimeout(done, ms); }) : Promise.resolve());
  return manager;
}

export async function loadModel(url) {
  const ext = url.split("?")[0].split(".").pop().toLowerCase();
  const manager = textureManager(url);
  if (ext === "obj") {
    const object = await new OBJLoader(manager).loadAsync(url);
    await manager.settled();
    return { object, clips: [] };
  }
  if (ext === "fbx") {
    const object = await new FBXLoader(manager).loadAsync(url);
    await manager.settled();
    return { object, clips: object.animations || [] };
  }
  const gltf = await new GLTFLoader(manager).loadAsync(url);
  await manager.settled();
  return { object: gltf.scene, clips: gltf.animations || [] };
}

function lights(scene) {
  scene.add(new THREE.HemisphereLight(0xffffff, 0x2a2a30, 1.6));
  const key = new THREE.DirectionalLight(0xffffff, 2.2);
  key.position.set(3, 5, 4);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xffd6e0, 0.8);
  rim.position.set(-4, 3, -4);
  scene.add(rim);
}

// Centre the model on the ground at the origin and return its size.
function normalise(object) {
  const box = new THREE.Box3().setFromObject(object);
  const size = box.getSize(new THREE.Vector3());
  const centre = box.getCenter(new THREE.Vector3());
  object.position.sub(new THREE.Vector3(centre.x, box.min.y, centre.z));
  return size;
}

function aim(camera, size, angle, ortho) {
  const a = ANGLES[angle] || ANGLES.front;
  const radius = Math.max(size.x, size.y, size.z) * (ortho ? 1.6 : 2.6) + 0.001;
  const az = THREE.MathUtils.degToRad(a.azimuth);
  const el = THREE.MathUtils.degToRad(a.elevation);
  const target = new THREE.Vector3(0, size.y / 2, 0);
  camera.position.set(Math.sin(az) * Math.cos(el) * radius, target.y + Math.sin(el) * radius, Math.cos(az) * Math.cos(el) * radius);
  if (ortho) {
    const half = Math.max(size.x, size.y, size.z) * 0.62;
    Object.assign(camera, { left: -half, right: half, top: half, bottom: -half, near: 0.01, far: radius * 4 });
  } else {
    camera.near = radius / 100;
    camera.far = radius * 10;
  }
  camera.lookAt(target);
  camera.updateProjectionMatrix();
  return target;
}

// Interactive viewer inside `container`. Returns controls for clips and stills.
export async function createViewer(container, url) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  lights(scene);
  const grid = new THREE.GridHelper(10, 20, 0x3f3f46, 0x27272a);
  scene.add(grid);
  const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1000);
  const { object, clips } = await loadModel(url);
  scene.add(object);
  const size = normalise(object);
  grid.scale.setScalar(Math.max(size.x, size.z, size.y) / 3 || 1);
  const target = aim(camera, size, "iso", false);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(target);
  controls.enableDamping = true;
  const mixer = new THREE.AnimationMixer(object);
  let action = null;
  const clock = new THREE.Clock();
  let running = true;
  const resize = () => {
    const { clientWidth: w, clientHeight: h } = container;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(1, h);
    camera.updateProjectionMatrix();
  };
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();
  (function loop() {
    if (!running) return;
    requestAnimationFrame(loop);
    mixer.update(clock.getDelta());
    controls.update();
    renderer.render(scene, camera);
  })();
  const stats = { triangles: 0, meshes: 0, bones: 0 };
  object.traverse((o) => {
    if (o.isMesh) { stats.meshes += 1; stats.triangles += (o.geometry.index ? o.geometry.index.count : o.geometry.attributes.position.count) / 3; }
    if (o.isBone) stats.bones += 1;
  });
  return {
    clips: clips.map((c) => c.name || "Clip"),
    stats: { ...stats, triangles: Math.round(stats.triangles), height: size.y },
    play(index) {
      action?.stop();
      action = index >= 0 && clips[index] ? mixer.clipAction(clips[index]).play() : null;
    },
    still() {
      grid.visible = false;
      renderer.render(scene, camera);
      const data = renderer.domElement.toDataURL("image/png");
      grid.visible = true;
      return data;
    },
    dispose() {
      running = false;
      observer.disconnect();
      controls.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}

function offscreen(size) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
  renderer.setSize(size, size, false);
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  return renderer;
}

// A square, transparent preview picture of the model.
export async function renderThumb(url, size = 512) {
  const renderer = offscreen(size);
  const scene = new THREE.Scene();
  lights(scene);
  const { object } = await loadModel(url);
  scene.add(object);
  const dims = normalise(object);
  const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1000);
  aim(camera, dims, "iso", false);
  renderer.render(scene, camera);
  const data = renderer.domElement.toDataURL("image/png");
  renderer.dispose();
  return data;
}

// Sprite sheet: one row per direction, one column per animation frame.
export async function renderSpriteSheet(url, { clip = -1, frames = 8, directions = 8, size = 128, angle = "iso" } = {}) {
  const renderer = offscreen(size);
  const scene = new THREE.Scene();
  lights(scene);
  const { object, clips } = await loadModel(url);
  const pivot = new THREE.Group();
  pivot.add(object);
  scene.add(pivot);
  const dims = normalise(object);
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.01, 100);
  aim(camera, dims, angle, true);
  const mixer = new THREE.AnimationMixer(object);
  const anim = clips[clip];
  const action = anim ? mixer.clipAction(anim).play() : null;
  const cols = anim ? Math.max(1, Math.min(64, frames)) : 1;
  const rows = Math.max(1, Math.min(16, directions));
  const sheet = document.createElement("canvas");
  sheet.width = cols * size;
  sheet.height = rows * size;
  const ctx = sheet.getContext("2d");
  for (let row = 0; row < rows; row += 1) {
    pivot.rotation.y = (row / rows) * Math.PI * 2;
    for (let col = 0; col < cols; col += 1) {
      if (action) mixer.setTime((anim.duration * col) / cols);
      renderer.render(scene, camera);
      ctx.drawImage(renderer.domElement, col * size, row * size);
    }
  }
  renderer.dispose();
  return {
    dataUrl: sheet.toDataURL("image/png"),
    meta: {
      frame_width: size, frame_height: size, columns: cols, rows,
      directions: rows, frames_per_direction: cols, angle,
      clip: anim?.name || "", clip_seconds: anim ? Number(anim.duration.toFixed(3)) : 0,
      fps: anim ? Math.round(cols / Math.max(anim.duration, 0.001)) : 0,
      direction_order: "row 0 is the camera view, then the model turns counter-clockwise seen from above",
    },
  };
}
