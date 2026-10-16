import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';

export class ModelViewer {
  constructor(container, onLoad) {
    this.container = container;
    this.onLoad = onLoad;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(36, 1, .01, 10000);
    this.camera.up.set(0, 0, 1);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setClearColor(0xf5f7f9, 0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    container.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = .08;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x95a6a4, 2.8));
    const light = new THREE.DirectionalLight(0xffffff, 3.2);
    light.position.set(2, -3, 5);
    this.scene.add(light);
    const fill = new THREE.DirectionalLight(0xe3edff, 1.8);
    fill.position.set(-2, 3, 2);
    this.scene.add(fill);
    this.wireframe = false;
    this.showGrid = true;
    this.generation = 0;
    this.dimensions = null;
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.renderer.setAnimationLoop(() => { this.controls.update(); this.renderer.render(this.scene, this.camera); });
  }
  resize() {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }
  async load(url) {
    const generation = ++this.generation;
    this.abort?.abort();
    this.abort = new AbortController();
    const response = await fetch(url, { signal: this.abort.signal });
    if (!response.ok) throw new Error('无法读取模型文件');
    const buffer = await response.arrayBuffer();
    if (generation !== this.generation) return;
    const geometry = new STLLoader().parse(buffer);
    geometry.computeBoundingBox();
    if (!geometry.attributes.position?.count || !Number.isFinite(geometry.boundingBox.max.x)) { geometry.dispose(); throw new Error('模型文件尚未写入完成'); }
    const size = geometry.boundingBox.getSize(new THREE.Vector3());
    const center = geometry.boundingBox.getCenter(new THREE.Vector3());
    geometry.translate(-center.x, -center.y, -center.z);
    geometry.computeVertexNormals();
    this.removeModel();
    const material = new THREE.MeshStandardMaterial({ color: 0xabbeb8, metalness: .3, roughness: .36, side: THREE.DoubleSide, wireframe: this.wireframe });
    this.mesh = new THREE.Mesh(geometry, material);
    this.scene.add(this.mesh);
    this.dimensions = size;
    const max = Math.max(size.x, size.y, size.z, .1);
    this.floor = new THREE.GridHelper(max * 3, 30, 0xd0dcd6, 0xe2e8e5);
    this.floor.rotation.x = Math.PI / 2;
    this.floor.position.z = -size.z / 2 - max * .008;
    this.floor.visible = this.showGrid;
    this.floor.material.transparent = true;
    this.floor.material.opacity = .65;
    this.scene.add(this.floor);
    this.camera.near = max / 1000;
    this.camera.far = max * 200;
    this.controls.minDistance = max * .1;
    this.controls.maxDistance = max * 30;
    this.setView('iso');
    this.onLoad(size);
  }
  setView(view = 'iso') {
    if (!this.dimensions) return;
    this.resize();
    const max = Math.max(this.dimensions.x, this.dimensions.y, this.dimensions.z, .1);
    const d = max / (2 * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))) * 1.55 / Math.min(this.camera.aspect, 1);
    const dirs = { iso: [1.4, -1.8, 1.25], front: [0, -1, .00001], top: [0, -.00001, 1], right: [1, 0, .00001] };
    this.camera.position.copy(new THREE.Vector3(...(dirs[view] || dirs.iso)).normalize().multiplyScalar(d));
    this.controls.target.set(0, 0, 0);
    this.camera.lookAt(0, 0, 0);
    this.camera.updateProjectionMatrix();
    this.controls.update();
  }
  toggleWireframe() { this.wireframe = !this.wireframe; if (this.mesh) this.mesh.material.wireframe = this.wireframe; return this.wireframe; }
  toggleGrid() { this.showGrid = !this.showGrid; if (this.floor) this.floor.visible = this.showGrid; return this.showGrid; }
  removeModel() {
    if (this.mesh) { this.scene.remove(this.mesh); this.mesh.geometry.dispose(); this.mesh.material.dispose(); this.mesh = null; }
    if (this.floor) { this.scene.remove(this.floor); this.floor.geometry.dispose(); this.floor.material.dispose(); this.floor = null; }
  }
  clear() { ++this.generation; this.abort?.abort(); this.removeModel(); this.dimensions = null; }
}
