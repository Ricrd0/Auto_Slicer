import {
  AmbientLight,
  BufferAttribute,
  BufferGeometry,
  Color,
  DirectionalLight,
  DoubleSide,
  Line,
  LineBasicMaterial,
  LineLoop,
  Mesh,
  MeshStandardMaterial,
  PerspectiveCamera,
  Plane,
  Raycaster,
  Scene,
  Vector2,
  Vector3,
  WebGLRenderer,
} from "three";

const state = {
  settings: null,
  options: null,
  printers: [],
  locations: { input_dir: "", output_dir: "", included: null },
  listing: null,
  models: [],
  groups: [],
  poses: {},
  selectedModel: null,
  mode: "orbit",
  drag: null,
  gcodePath: null,
  gcodeLayers: 0,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      /* response has no json body */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) return response.json();
  return response;
}

function rotateVertex(x, y, z, rx, ry, rz) {
  const ax = (rx * Math.PI) / 180;
  const ay = (ry * Math.PI) / 180;
  const az = (rz * Math.PI) / 180;
  const cx = Math.cos(ax);
  const sx = Math.sin(ax);
  const cy = Math.cos(ay);
  const sy = Math.sin(ay);
  const cz = Math.cos(az);
  const sz = Math.sin(az);
  const y1 = cx * y - sx * z;
  const z1 = sx * y + cx * z;
  const x2 = cy * x + sy * z1;
  const z2 = -sy * x + cy * z1;
  const x3 = cz * x2 - sz * y1;
  const y3 = sz * x2 + cz * y1;
  return [x3, y3, z2];
}

function parseStl(buffer) {
  const view = new DataView(buffer);
  const count = view.getUint32(80, true);
  const positions = new Float32Array(count * 9);
  let offset = 84;
  let write = 0;
  for (let index = 0; index < count; index += 1) {
    offset += 12;
    for (let vertex = 0; vertex < 3; vertex += 1) {
      positions[write] = view.getFloat32(offset, true);
      positions[write + 1] = view.getFloat32(offset + 4, true);
      positions[write + 2] = view.getFloat32(offset + 8, true);
      write += 3;
      offset += 12;
    }
    offset += 2;
  }
  return positions;
}

function geometryFromPositions(positions, rotation) {
  const rotated = new Float32Array(positions.length);
  for (let index = 0; index < positions.length; index += 3) {
    const vertex = rotateVertex(
      positions[index],
      positions[index + 1],
      positions[index + 2],
      rotation[0],
      rotation[1],
      rotation[2],
    );
    rotated[index] = vertex[0];
    rotated[index + 1] = vertex[1];
    rotated[index + 2] = vertex[2];
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new BufferAttribute(rotated, 3));
  geometry.computeVertexNormals();
  geometry.computeBoundingBox();
  const box = geometry.boundingBox;
  geometry.translate(-box.min.x, -box.min.y, -box.min.z);
  return geometry;
}

const preview = setupViewport($("preview"), 0x241f1a);
const gcodeView = setupViewport($("gcode-view"), 0x16130f);
const bed = new LineLoop(
  new BufferGeometry(),
  new LineBasicMaterial({ color: 0xf4e1c1 }),
);
preview.scene.add(bed);
const partMaterial = new MeshStandardMaterial({ color: 0xc4a484, roughness: 0.7, metalness: 0.0, side: DoubleSide });
const selectedMaterial = new MeshStandardMaterial({ color: 0xe7c8a0, roughness: 0.55, metalness: 0.0, side: DoubleSide });

function setupViewport(canvas, clear) {
  const renderer = new WebGLRenderer({ canvas, antialias: true });
  renderer.setClearColor(clear, 1);
  const scene = new Scene();
  scene.add(new AmbientLight(0xffffff, 0.65));
  const sun = new DirectionalLight(0xffffff, 1.1);
  sun.position.set(40, -80, 120);
  scene.add(sun);
  const camera = new PerspectiveCamera(40, 1, 0.1, 5000);
  const orbit = { theta: 0.7, phi: 1.05, radius: 280, target: new Vector3(100, 100, 0) };
  function frame() {
    const width = canvas.clientWidth || 640;
    const height = canvas.clientHeight || 460;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    const target = orbit.target;
    camera.position.set(
      target.x + orbit.radius * Math.sin(orbit.phi) * Math.sin(orbit.theta),
      target.y - orbit.radius * Math.sin(orbit.phi) * Math.cos(orbit.theta),
      target.z + orbit.radius * Math.cos(orbit.phi),
    );
    camera.up.set(0, 0, 1);
    camera.lookAt(target);
    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  return { canvas, renderer, scene, camera, orbit };
}

function setBed(width, depth) {
  const points = [new Vector3(0, 0, 0), new Vector3(width, 0, 0), new Vector3(width, depth, 0), new Vector3(0, depth, 0)];
  bed.geometry.dispose();
  bed.geometry = new BufferGeometry().setFromPoints(points);
  preview.orbit.target.set(width / 2, depth / 2, 0);
  preview.orbit.radius = Math.max(width, depth) * 1.3;
}

function clearParts() {
  for (const child of [...preview.scene.children]) {
    if (child.userData.file) {
      child.geometry.dispose();
      preview.scene.remove(child);
    }
  }
}

async function loadMesh(path) {
  const response = await fetch(`/api/models/mesh?path=${encodeURIComponent(path)}`);
  if (!response.ok) throw new Error(`could not load ${path}`);
  return parseStl(await response.arrayBuffer());
}

function effectiveRotation(file) {
  const pose = state.poses[file];
  if (pose && pose.rotation) return pose.rotation;
  return [state.settings.rotation_x, state.settings.rotation_y, state.settings.rotation_z];
}

async function showLayout() {
  const printer = $("preview-printer").value;
  const error = $("layout-error");
  error.textContent = "";
  clearParts();
  if (!printer) return;
  const kind = $("preview-kind").value;
  const body = { printer_id: printer, kind };
  if (kind === "model") {
    if (!state.selectedModel) return;
    body.model = state.selectedModel;
  } else {
    body.group_id = $("preview-group").value;
    if (!body.group_id) return;
  }
  try {
    const layout = await api("/api/layout", { method: "POST", body: JSON.stringify(body) });
    setBed(layout.bed_width, layout.bed_depth);
    if (layout.error) error.textContent = layout.error;
    for (const item of layout.items) {
      const positions = await loadMesh(item.file);
      const geometry = geometryFromPositions(positions, item.rotation);
      const mesh = new Mesh(geometry, item.file === state.selectedModel ? selectedMaterial : partMaterial);
      mesh.position.set(item.min_x, item.min_y, 0);
      mesh.userData.file = item.file;
      mesh.userData.positions = positions;
      preview.scene.add(mesh);
    }
    renderPoseFields();
  } catch (caught) {
    error.textContent = caught.message;
  }
}

function renderPoseFields() {
  const root = $("pose-fields");
  root.innerHTML = "";
  const file = state.selectedModel;
  if (!file) return;
  const rotation = effectiveRotation(file);
  for (const [index, axis] of ["X", "Y", "Z"].entries()) {
    const label = document.createElement("label");
    label.textContent = `Rotation ${axis}`;
    const input = document.createElement("input");
    input.type = "number";
    input.step = "1";
    input.value = String(Math.round(rotation[index] * 1000) / 1000);
    input.addEventListener("change", async () => {
      const next = effectiveRotation(file).slice();
      next[index] = Number(input.value);
      state.poses[file] = { ...(state.poses[file] || {}), rotation: next };
      await api("/api/poses", { method: "PUT", body: JSON.stringify({ path: file, rotation: next }) });
      await showLayout();
    });
    label.append(input);
    root.append(label);
  }
}

function bindPreviewPointer() {
  const canvas = preview.canvas;
  const raycaster = new Raycaster();
  const pointer = new Vector2();
  const plane = new Plane(new Vector3(0, 0, 1), 0);
  const hit = new Vector3();

  function pointerRay(event) {
    const rect = canvas.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, preview.camera);
  }

  canvas.addEventListener("pointerdown", (event) => {
    pointerRay(event);
    if (state.mode === "orbit") {
      state.drag = { kind: "orbit", x: event.clientX, y: event.clientY, ...preview.orbit };
      return;
    }
    const meshes = preview.scene.children.filter((child) => child.userData.file);
    const intersections = raycaster.intersectObjects(meshes, false);
    if (!intersections.length || !raycaster.ray.intersectPlane(plane, hit)) return;
    const mesh = intersections[0].object;
    state.selectedModel = mesh.userData.file;
    if (state.mode === "move") {
      state.drag = {
        kind: "move",
        mesh,
        offsetX: hit.x - mesh.position.x,
        offsetY: hit.y - mesh.position.y,
      };
    } else {
      const rotation = effectiveRotation(mesh.userData.file).slice();
      const axis = $("rotate-axis").value;
      state.drag = {
        kind: "rotate",
        mesh,
        file: mesh.userData.file,
        startX: event.clientX,
        base: rotation,
        positions: mesh.userData.positions,
        index: "XYZ".indexOf(axis),
      };
    }
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener("pointermove", (event) => {
    if (!state.drag) return;
    if (state.drag.kind === "orbit") {
      preview.orbit.theta = state.drag.theta + (event.clientX - state.drag.x) * 0.01;
      preview.orbit.phi = Math.min(1.4, Math.max(0.25, state.drag.phi + (event.clientY - state.drag.y) * 0.01));
      return;
    }
    if (state.drag.kind === "move") {
      pointerRay(event);
      if (!raycaster.ray.intersectPlane(plane, hit)) return;
      state.drag.mesh.position.x = hit.x - state.drag.offsetX;
      state.drag.mesh.position.y = hit.y - state.drag.offsetY;
      return;
    }
    const next = state.drag.base.slice();
    next[state.drag.index] = state.drag.base[state.drag.index] + (event.clientX - state.drag.startX) * 0.4;
    state.poses[state.drag.file] = { ...(state.poses[state.drag.file] || {}), rotation: next };
    const geometry = geometryFromPositions(state.drag.positions, next);
    state.drag.mesh.geometry.dispose();
    state.drag.mesh.geometry = geometry;
    return;
  });

  canvas.addEventListener("pointerup", async () => {
    const drag = state.drag;
    state.drag = null;
    if (!drag || drag.kind === "orbit") return;
    if (drag.kind === "move") {
      const file = drag.mesh.userData.file;
      const position = [drag.mesh.position.x, drag.mesh.position.y];
      if ($("preview-kind").value === "group") {
        const layout = await api("/api/groups/layout", {
          method: "PUT",
          body: JSON.stringify({
            id: $("preview-group").value,
            printer_id: $("preview-printer").value,
            file,
            x: position[0],
            y: position[1],
          }),
        });
        const group = state.groups.find((item) => item.id === $("preview-group").value);
        if (group) {
          group.manual_layout = Object.fromEntries(layout.items.map((item) => [item.file, { x: item.min_x, y: item.min_y }]));
        }
      } else {
        state.poses[file] = { ...(state.poses[file] || {}), position };
        await api("/api/poses", { method: "PUT", body: JSON.stringify({ path: file, position }) });
      }
    } else {
      await api("/api/poses", {
        method: "PUT",
        body: JSON.stringify({ path: drag.file, rotation: state.poses[drag.file]?.rotation || drag.base }),
      });
    }
    await showLayout();
  });

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    preview.orbit.radius = Math.max(40, preview.orbit.radius * (event.deltaY > 0 ? 1.08 : 0.92));
  }, { passive: false });
}

function field(name, label, type, extra = {}) {
  const wrap = document.createElement("label");
  wrap.textContent = label;
  const input = document.createElement(type === "select" ? "select" : "input");
  if (type !== "select") input.type = type;
  input.name = name;
  if (extra.step) input.step = extra.step;
  if (type === "checkbox") wrap.classList.add("inline");
  if (extra.options) {
    for (const option of extra.options) {
      const node = document.createElement("option");
      node.value = option;
      node.textContent = extra.labels?.[option] || option.replaceAll("_", " ");
      input.append(node);
    }
  }
  wrap.append(input);
  return wrap;
}

function renderSettings() {
  const form = $("settings-form");
  const options = state.options;
  const specs = [
    ["slicer_engine", "Slicing engine", "select", { options: options.slicer_engines, labels: { cura: "Cura", orca: "Orca" } }],
    ["output_folder_name", "Output folder name", "text"],
    ["rotation_x", "Shared rotation X", "number", { step: "1" }],
    ["rotation_y", "Shared rotation Y", "number", { step: "1" }],
    ["rotation_z", "Shared rotation Z", "number", { step: "1" }],
    ["layer_height", "Layer height (mm)", "number", { step: "0.01" }],
    ["ironing_enabled", "Ironing", "checkbox"],
    ["ironing_only_highest_layer", "Ironing top layer only", "checkbox"],
    ["z_seam_type", "Seam", "select", { options: options.seam_types }],
    ["z_seam_position", "Seam position", "select", { options: options.seam_positions }],
    ["infill_pattern", "Infill", "select", { options: options.infill_patterns }],
    ["infill_sparse_density", "Infill density (%)", "number", { step: "1" }],
    ["retraction_combing", "Combing", "select", { options: options.combing_modes }],
    ["adhesion_type", "Bed adhesion", "select", { options: options.adhesion_types }],
    ["brim_width", "Brim width (mm)", "number", { step: "0.1" }],
    ["skirt_line_count", "Skirt lines", "number", { step: "1" }],
    ["raft_margin", "Raft margin (mm)", "number", { step: "0.1" }],
    ["support_enable", "Supports", "checkbox"],
    ["support_type", "Support placement", "select", { options: options.support_types }],
    ["support_structure", "Support structure", "select", { options: options.support_structures }],
    ["support_angle", "Support angle", "number", { step: "1" }],
    ["support_infill_rate", "Support density (%)", "number", { step: "1" }],
  ];
  form.innerHTML = "";
  for (const [name, label, type, extra] of specs) {
    form.append(field(name, label, type, extra));
  }
  for (const input of form.querySelectorAll("input, select")) {
    const value = state.settings[input.name];
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
    input.addEventListener("change", saveSettings);
  }
}

async function saveSettings() {
  const form = $("settings-form");
  const previousEngine = state.settings.slicer_engine;
  const payload = { ...state.settings };
  for (const input of form.querySelectorAll("input, select")) {
    if (input.type === "checkbox") payload[input.name] = input.checked;
    else if (input.type === "number") payload[input.name] = Number(input.value);
    else payload[input.name] = input.value;
  }
  state.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
  if (state.settings.slicer_engine !== previousEngine) {
    state.printers = await api("/api/printers");
    renderPrinters();
  }
  if ($("models").classList.contains("active")) await showLayout();
}

function renderPrinters() {
  const notice = $("printer-notice");
  const engine = state.settings?.slicer_engine || "cura";
  const root = engine === "orca" ? state.health?.orca_config_dir : state.health?.config_dir;
  if (!state.printers.length) {
    notice.textContent = engine === "orca"
      ? `No Orca printers${root ? ` in ${root}` : ""}. Orca keeps machine profiles under %APPDATA%\\OrcaSlicer.`
      : root
        ? `No printers in ${root}. Cura keeps machine profiles in a version folder such as %APPDATA%\\cura\\5.13 (the folder that contains machine_instances).`
        : "No printers found.";
  } else {
    notice.textContent = root ? `Loaded from ${root}` : "";
  }
  const body = $("printer-rows");
  body.innerHTML = "";
  const select = $("preview-printer");
  const previous = select.value;
  select.innerHTML = "";
  for (const printer of state.printers) {
    const option = document.createElement("option");
    option.value = printer.id;
    option.textContent = printer.name;
    select.append(option);
    const row = document.createElement("tr");
    const notes = [printer.error, ...(printer.warnings || [])].filter(Boolean).join(" ");
    row.innerHTML = `<td><input type="checkbox" ${printer.enabled ? "checked" : ""} ${printer.slicable ? "" : "disabled"}></td>
      <td>${printer.name}</td>
      <td>${printer.machine_width ?? "?"} × ${printer.machine_depth ?? "?"} × ${printer.machine_height ?? "?"}</td>
      <td class="${printer.error ? "" : "muted"}">${notes}</td>
      <td><a href="/api/bundles?printer_id=${encodeURIComponent(printer.id)}">Download</a></td>`;
    row.querySelector("input").addEventListener("change", async (event) => {
      await api("/api/printers/enabled", {
        method: "PUT",
        body: JSON.stringify({ id: printer.id, enabled: event.target.checked }),
      });
    });
    body.append(row);
  }
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}

function renderPaths() {
  const input = $("input-path");
  const output = $("output-path");
  if (document.activeElement !== input) input.value = state.locations?.input_dir || "";
  if (document.activeElement !== output) output.value = state.locations?.output_dir || "";
}

async function commitPath(field, value) {
  $("path-error").textContent = "";
  try {
    state.locations = await api("/api/locations", {
      method: "PUT",
      body: JSON.stringify({ [field]: value }),
    });
    renderPaths();
    if (field === "input_dir") {
      state.models = await api("/api/models");
      state.selectedModel = null;
      renderModels();
      if ($("models").classList.contains("active")) await showLayout();
    }
  } catch (error) {
    $("path-error").textContent = error.message;
  }
}

function renderModels() {
  const list = $("model-list");
  list.innerHTML = "";
  if (!state.models.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No STL or 3MF files in this folder. Browse to a folder that contains models, or paste its path above.";
    list.append(empty);
  }
  for (const model of state.models) {
    const row = document.createElement("div");
    row.className = "model-row";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = model.included !== false;
    box.title = "Include this file when slicing";
    box.addEventListener("change", async () => {
      const included = state.models
        .filter((item) => (item.path === model.path ? box.checked : item.included !== false))
        .map((item) => item.path);
      state.locations = await api("/api/locations", {
        method: "PUT",
        body: JSON.stringify({ included }),
      });
      state.models = await api("/api/models");
      renderModels();
    });
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = model.path;
    if (model.path === state.selectedModel) button.classList.add("primary");
    button.addEventListener("click", async () => {
      state.selectedModel = model.path;
      $("preview-kind").value = "model";
      renderModels();
      await showLayout();
    });
    row.append(box, button);
    list.append(row);
  }
  const groupSelect = $("preview-group");
  groupSelect.innerHTML = "";
  const host = $("group-list");
  host.innerHTML = "";
  for (const group of state.groups) {
    const option = document.createElement("option");
    option.value = group.id;
    option.textContent = group.name;
    groupSelect.append(option);
    const block = document.createElement("div");
    block.className = "group-block";
    const title = document.createElement("strong");
    title.textContent = group.name;
    block.append(title);
    for (const model of state.models) {
      const label = document.createElement("label");
      label.className = "inline";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = group.files.includes(model.path);
      box.addEventListener("change", async () => {
        group.files = box.checked
          ? [...group.files, model.path]
          : group.files.filter((file) => file !== model.path);
        state.groups = (await api("/api/groups", { method: "PUT", body: JSON.stringify({ groups: state.groups }) })).groups;
        await showLayout();
      });
      label.append(box, document.createTextNode(model.path));
      block.append(label);
    }
    host.append(block);
  }
  if (!state.selectedModel && state.models.length) state.selectedModel = state.models[0].path;
}

function renderJob(job) {
  $("job-status").textContent = job && job.status ? `${job.kind} ${job.status}` : "No job yet";
  const body = $("job-rows");
  body.innerHTML = "";
  for (const item of job?.items || []) {
    const row = document.createElement("tr");
    const time = item.time_seconds == null ? "" : formatDuration(item.time_seconds);
    const filament = item.filament_meters == null ? "" : `${item.filament_meters.toFixed(2)} m`;
    row.innerHTML = `<td>${item.printer_name}</td><td>${item.item_type}: ${item.item_name}</td><td>${item.status}${item.progress ? ` — ${item.progress}` : ""}${item.error ? ` — ${item.error}` : ""}</td><td>${time}</td><td>${filament}</td><td></td>`;
    if (item.status === "done" && item.output_path) {
      const link = document.createElement("a");
      link.href = `/api/output?path=${encodeURIComponent(item.output_path)}`;
      link.textContent = "Download";
      row.lastElementChild.append(link);
      if (job.kind === "test") {
        const view = document.createElement("button");
        view.type = "button";
        view.textContent = "View";
        view.addEventListener("click", () => loadGcode(item.output_path));
        row.lastElementChild.append(view);
      }
    }
    body.append(row);
  }
  const testItem = (job?.items || []).find((item) => job.kind === "test" && item.status === "done");
  if (testItem && state.gcodePath !== testItem.output_path) loadGcode(testItem.output_path);
}

function formatDuration(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

async function loadGcode(path) {
  state.gcodePath = path;
  const info = await api(`/api/gcode/info?path=${encodeURIComponent(path)}`);
  state.gcodeLayers = info.layer_count;
  const slider = $("layer-slider");
  slider.max = String(Math.max(0, info.layer_count - 1));
  slider.value = "0";
  await drawLayer(0);
}

async function drawLayer(index) {
  if (!state.gcodePath) return;
  $("layer-label").textContent = `${index + 1} / ${state.gcodeLayers}`;
  const include = $("show-travel").checked;
  const layer = await api(`/api/gcode/layer?path=${encodeURIComponent(state.gcodePath)}&index=${index}&include_travel=${include}`);
  for (const child of [...gcodeView.scene.children]) {
    if (child.userData.toolpath) {
      child.geometry.dispose();
      gcodeView.scene.remove(child);
    }
  }
  const colors = {
    "WALL-OUTER": 0xf2c14e,
    "WALL-INNER": 0xe09f3e,
    SKIN: 0xfff3bf,
    FILL: 0x4cc9f0,
    SUPPORT: 0x90be6d,
    "SUPPORT-INTERFACE": 0x43aa8b,
    TRAVEL: 0x666666,
  };
  let maxSpan = 10;
  for (const polyline of layer.polylines) {
    const points = polyline.points.map((point) => new Vector3(point[0], point[1], point[2]));
    for (const point of points) maxSpan = Math.max(maxSpan, point.x, point.y, point.z);
    const line = new Line(
      new BufferGeometry().setFromPoints(points),
      new LineBasicMaterial({ color: new Color(colors[polyline.type] || 0xffffff) }),
    );
    line.userData.toolpath = true;
    gcodeView.scene.add(line);
  }
  gcodeView.orbit.target.set(maxSpan / 4, maxSpan / 4, 0);
  gcodeView.orbit.radius = maxSpan * 1.4;
}

function setMode(mode) {
  state.mode = mode;
  for (const [id, name] of [["mode-orbit", "orbit"], ["mode-move", "move"], ["mode-rotate", "rotate"]]) {
    $(id).classList.toggle("primary", name === mode);
  }
}

document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((panel) => panel.classList.remove("active"));
    button.classList.add("active");
    $(button.dataset.tab).classList.add("active");
  });
});

$("refresh-printers").addEventListener("click", async () => {
  state.health = await api("/api/health");
  state.printers = await api("/api/printers");
  renderPrinters();
});
$("upload-bundle").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  const response = await fetch("/api/bundles", { method: "POST", body });
  if (!response.ok) throw new Error("upload failed");
  state.health = await api("/api/health");
  state.printers = await api("/api/printers");
  renderPrinters();
});
$("refresh-models").addEventListener("click", async () => {
  state.models = await api("/api/models");
  renderModels();
  await showLayout();
});
$("input-path").addEventListener("change", () => commitPath("input_dir", $("input-path").value));
$("output-path").addEventListener("change", () => commitPath("output_dir", $("output-path").value));
$("browse-input").addEventListener("click", () => openBrowser("input"));
$("browse-output").addEventListener("click", () => openBrowser("output"));
$("browser-close").addEventListener("click", () => $("browser").close());
$("browser-up").addEventListener("click", () => loadBrowser(state.listing?.parent || ""));
$("browser-go").addEventListener("click", () => loadBrowser($("browser-path").value));
$("browser-path").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    loadBrowser($("browser-path").value);
  }
});
$("browser-use").addEventListener("click", () => applyBrowser(false));
$("browser-files").addEventListener("click", () => applyBrowser(true));

async function openBrowser(purpose) {
  state.browserPurpose = purpose;
  $("browser-title").textContent = purpose === "input" ? "Choose input folder" : "Choose output directory";
  $("browser-files").hidden = purpose !== "input";
  $("browser-error").textContent = "";
  $("browser").showModal();
  const start = purpose === "input" ? $("input-path").value : $("output-path").value;
  await loadBrowser(start);
}

async function loadBrowser(path) {
  $("browser-error").textContent = "";
  try {
    const listing = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
    state.listing = listing;
    $("browser-path").value = listing.path || "";
    const list = $("browser-list");
    list.innerHTML = "";
    if (!listing.entries.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = path ? "This folder has no subfolders or model files." : "No drives are visible.";
      list.append(empty);
      return;
    }
    for (const entry of listing.entries) {
      const row = document.createElement("div");
      row.className = "browser-row";
      if (entry.kind === "file" && state.browserPurpose === "input") {
        const box = document.createElement("input");
        box.type = "checkbox";
        box.dataset.file = entry.name;
        row.append(box);
      } else {
        const spacer = document.createElement("span");
        spacer.className = "browser-spacer";
        row.append(spacer);
      }
      const button = document.createElement("button");
      button.type = "button";
      const folder = entry.kind === "dir" && !entry.name.endsWith("\\") ? `${entry.name}\\` : entry.name;
      button.textContent = folder;
      if (entry.kind === "dir") button.addEventListener("click", () => loadBrowser(entry.path));
      row.append(button);
      list.append(row);
    }
  } catch (error) {
    $("browser-error").textContent = error.message;
  }
}

async function applyBrowser(filesOnly) {
  const path = $("browser-path").value;
  const field = state.browserPurpose === "output" ? "output_dir" : "input_dir";
  const body = { [field]: path };
  if (field === "input_dir") {
    body.included = filesOnly
      ? [...$("browser-list").querySelectorAll("input:checked")].map((box) => box.dataset.file)
      : null;
    if (filesOnly && !body.included.length) {
      $("browser-error").textContent = "Select one or more STL or 3MF files.";
      return;
    }
  }
  try {
    state.locations = await api("/api/locations", { method: "PUT", body: JSON.stringify(body) });
    $("browser").close();
    renderPaths();
    $("path-error").textContent = "";
    if (field === "input_dir") {
      state.models = await api("/api/models");
      state.selectedModel = state.models[0]?.path || null;
      renderModels();
      if ($("models").classList.contains("active")) await showLayout();
    }
  } catch (error) {
    $("browser-error").textContent = error.message;
  }
}
$("add-group").addEventListener("click", async () => {
  const name = $("group-name").value.trim() || `group ${state.groups.length + 1}`;
  state.groups.push({ name, files: state.selectedModel ? [state.selectedModel] : [] });
  state.groups = (await api("/api/groups", { method: "PUT", body: JSON.stringify({ groups: state.groups }) })).groups;
  $("group-name").value = "";
  renderModels();
});
$("preview-printer").addEventListener("change", showLayout);
$("preview-kind").addEventListener("change", showLayout);
$("preview-group").addEventListener("change", showLayout);
$("mode-orbit").addEventListener("click", () => setMode("orbit"));
$("mode-move").addEventListener("click", () => setMode("move"));
$("mode-rotate").addEventListener("click", () => setMode("rotate"));
$("reset-pose").addEventListener("click", async () => {
  if (!state.selectedModel) return;
  delete state.poses[state.selectedModel];
  await api("/api/poses", { method: "PUT", body: JSON.stringify({ path: state.selectedModel, rotation: null, position: null }) });
  await showLayout();
});
$("reset-layout").addEventListener("click", async () => {
  const id = $("preview-group").value;
  if (!id) return;
  await api(`/api/groups/layout?id=${encodeURIComponent(id)}`, { method: "DELETE" });
  state.groups = (await api("/api/groups")).groups;
  await showLayout();
});
$("test-slice").addEventListener("click", async () => {
  try {
    const kind = $("preview-kind").value;
    const itemName = kind === "group" ? $("preview-group").value : state.selectedModel;
    const job = await api("/api/test", {
      method: "POST",
      body: JSON.stringify({ printer_id: $("preview-printer").value, item_type: kind, item_name: itemName }),
    });
    renderJob(job);
    document.querySelector('[data-tab="jobs"]').click();
    pollJob();
  } catch (error) {
    $("job-status").textContent = error.message;
    document.querySelector('[data-tab="jobs"]').click();
  }
});
$("start-batch").addEventListener("click", async () => {
  try {
    const job = await api("/api/jobs", { method: "POST" });
    renderJob(job);
    pollJob();
  } catch (error) {
    $("job-status").textContent = error.message;
  }
});
$("layer-slider").addEventListener("input", (event) => drawLayer(Number(event.target.value)));
$("show-travel").addEventListener("change", () => drawLayer(Number($("layer-slider").value)));

let pollTimer = null;
function pollJob() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const job = await api("/api/jobs/current");
    renderJob(job);
    if (!job.status || job.status === "completed" || job.status === "completed_with_errors") {
      clearInterval(pollTimer);
    }
  }, 1000);
}

bindPreviewPointer();

const boot = await Promise.all([
  api("/api/health"),
  api("/api/options"),
  api("/api/settings"),
  api("/api/printers"),
  api("/api/models"),
  api("/api/locations"),
  api("/api/groups"),
  api("/api/poses"),
  api("/api/jobs/current"),
]);
const curaReady = boot[0].cura_engine || boot[0].engine;
$("engine-status").textContent = `${curaReady ? "CuraEngine ready" : "CuraEngine was not found"} · ${boot[0].orca_engine ? "OrcaSlicer ready" : "OrcaSlicer was not found"}`;
state.health = boot[0];
state.options = boot[1];
state.settings = boot[2];
state.printers = boot[3];
state.models = boot[4];
state.locations = boot[5];
state.groups = boot[6].groups;
state.poses = boot[7];
renderSettings();
renderPrinters();
renderPaths();
renderModels();
renderJob(boot[8]);
await showLayout();
if (boot[8].status === "running" || boot[8].status === "queued") pollJob();
