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
  owned: { printers: [], orca_profiles: [] },
  filaments: { selected_id: null, filaments: [] },
  filamentEdit: null,
  filamentCatalog: [],
  filamentSearch: "idle",
  tempDraft: null,
  orcaCatalog: false,
  locations: { input_dir: "", output_dir: "", included: null },
  listing: null,
  models: [],
  groups: [],
  poses: {},
  selectedModel: null,
  layoutItems: [],
  mode: "orbit",
  drag: null,
  gcodePath: null,
  gcodeLabel: null,
  gcodeBed: null,
  gcodeLayers: 0,
  gcodeFramed: false,
  gcodeDrag: null,
  gcodeLayerRequest: 0,
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

function geometryFromPositions(positions, rotation, scale = 1) {
  const factor = scale > 0 ? scale : 1;
  const rotated = new Float32Array(positions.length);
  for (let index = 0; index < positions.length; index += 3) {
    const vertex = rotateVertex(
      positions[index] * factor,
      positions[index + 1] * factor,
      positions[index + 2] * factor,
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
const gcodeBed = new LineLoop(
  new BufferGeometry(),
  new LineBasicMaterial({ color: 0xf4e1c1 }),
);
gcodeBed.userData.bed = true;
gcodeView.scene.add(gcodeBed);
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
  preview.bedWidth = width;
  preview.bedDepth = depth;
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
  if (!printer) {
    state.layoutItems = [];
    renderPoseFields();
    return;
  }
  const kind = $("preview-kind").value;
  const body = { printer_id: printer, kind };
  if (kind === "model") {
    if (!state.selectedModel) {
      state.layoutItems = [];
      renderPoseFields();
      return;
    }
    body.model = state.selectedModel;
  } else {
    body.group_id = $("preview-group").value;
    if (!body.group_id) {
      state.layoutItems = [];
      renderPoseFields();
      return;
    }
  }
  try {
    const layout = await api("/api/layout", { method: "POST", body: JSON.stringify(body) });
    setBed(layout.bed_width, layout.bed_depth);
    state.layoutItems = layout.items;
    if (layout.error) error.textContent = layout.error;
    if (kind === "group") {
      const group = state.groups.find((entry) => entry.id === body.group_id);
      if (group) {
        if (layout.manual_layout) group.manual_layout = layout.manual_layout;
        if (layout.arranged_3mf !== undefined) group.arranged_3mf = layout.arranged_3mf;
      }
    }
    for (const item of layout.items) {
      const positions = await loadMesh(item.file);
      const geometry = geometryFromPositions(positions, item.rotation, item.scale || 1);
      const mesh = new Mesh(geometry, item.file === state.selectedModel ? selectedMaterial : partMaterial);
      mesh.position.set(item.min_x, item.min_y, item.z || 0);
      mesh.userData.file = item.file;
      mesh.userData.positions = positions;
      mesh.userData.scale = item.scale || 1;
      preview.scene.add(mesh);
    }
    renderPoseFields();
  } catch (caught) {
    error.textContent = caught.message;
  }
}

function poseNumber(value) {
  return String(Math.round(Number(value) * 1000) / 1000);
}

async function savePartField(file, key, value) {
  const item = (state.layoutItems || []).find((entry) => entry.file === file);
  if (!item || Number.isNaN(value)) return;
  const pose = { ...(state.poses[file] || {}) };
  if (key === "rx" || key === "ry" || key === "rz") {
    const rotation = (pose.rotation || item.rotation || [0, 0, 0]).slice();
    rotation[{ rx: 0, ry: 1, rz: 2 }[key]] = value;
    pose.rotation = rotation;
    state.poses[file] = pose;
    await api("/api/poses", { method: "PUT", body: JSON.stringify({ path: file, rotation }) });
  } else if (key === "scale") {
    const scale = value > 0 ? value : 1;
    pose.scale = scale;
    state.poses[file] = pose;
    await api("/api/poses", { method: "PUT", body: JSON.stringify({ path: file, scale }) });
  } else if (key === "z") {
    pose.z = value;
    state.poses[file] = pose;
    await api("/api/poses", { method: "PUT", body: JSON.stringify({ path: file, z: value }) });
  } else if ($("preview-kind").value === "group") {
    const layout = await api("/api/groups/layout", {
      method: "PUT",
      body: JSON.stringify({
        id: $("preview-group").value,
        printer_id: $("preview-printer").value,
        file,
        x: key === "x" ? value : item.min_x,
        y: key === "y" ? value : item.min_y,
      }),
    });
    const group = state.groups.find((entry) => entry.id === $("preview-group").value);
    if (group) {
      group.manual_layout = Object.fromEntries(layout.items.map((entry) => [entry.file, { x: entry.min_x, y: entry.min_y }]));
    }
  } else {
    const position = [key === "x" ? value : item.min_x, key === "y" ? value : item.min_y];
    pose.position = position;
    state.poses[file] = pose;
    await api("/api/poses", { method: "PUT", body: JSON.stringify({ path: file, position }) });
  }
  await showLayout();
}

function renderPoseFields() {
  const root = $("pose-fields");
  root.innerHTML = "";
  const items = state.layoutItems || [];
  if (!items.length) return;
  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = "X and Y are millimetres from the front-left of the bed to the front-left of the part. Z is the gap under the part. Scale is uniform.";
  root.append(note);
  for (const item of items) {
    const card = document.createElement("fieldset");
    card.className = "pose-part";
    const legend = document.createElement("legend");
    legend.textContent = item.file.split(/[/\\]/).pop();
    card.append(legend);
    const grid = document.createElement("div");
    grid.className = "pose-grid";
    const rotation = item.rotation || [0, 0, 0];
    const fields = [
      ["X (mm)", item.min_x, "x", "0.1"],
      ["Y (mm)", item.min_y, "y", "0.1"],
      ["Z (mm)", item.z || 0, "z", "0.1"],
      ["Rotation X", rotation[0], "rx", "1"],
      ["Rotation Y", rotation[1], "ry", "1"],
      ["Rotation Z", rotation[2], "rz", "1"],
      ["Scale", item.scale || 1, "scale", "0.01"],
    ];
    for (const [label, raw, key, step] of fields) {
      const wrap = document.createElement("label");
      wrap.textContent = label;
      const input = document.createElement("input");
      input.type = "number";
      input.step = step;
      if (key === "scale") input.min = "0.01";
      input.value = poseNumber(raw);
      input.addEventListener("change", () => savePartField(item.file, key, Number(input.value)));
      wrap.append(input);
      grid.append(wrap);
    }
    card.append(grid);
    root.append(card);
  }
}

function bedDirections() {
  preview.camera.updateMatrixWorld();
  const right = new Vector3().setFromMatrixColumn(preview.camera.matrixWorld, 0);
  right.z = 0;
  if (right.lengthSq() < 1e-6) right.set(1, 0, 0);
  else right.normalize();
  const forward = new Vector3().setFromMatrixColumn(preview.camera.matrixWorld, 2);
  forward.z = 0;
  forward.negate();
  if (forward.lengthSq() < 1e-6) forward.set(0, 1, 0);
  else forward.normalize();
  return { right, forward };
}

function bindPreviewPointer() {
  const canvas = preview.canvas;
  const raycaster = new Raycaster();
  const pointer = new Vector2();

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
    if (!intersections.length) return;
    const mesh = intersections[0].object;
    state.selectedModel = mesh.userData.file;
    if (state.mode === "move") {
      state.drag = {
        kind: "move",
        mesh,
        startX: event.clientX,
        startY: event.clientY,
        originX: mesh.position.x,
        originY: mesh.position.y,
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
      const { right, forward } = bedDirections();
      const dx = event.clientX - state.drag.startX;
      const dy = event.clientY - state.drag.startY;
      const mmPerPixel = ((preview.bedWidth || 220) / Math.max(canvas.clientWidth, 1)) * 0.45;
      state.drag.mesh.position.x = state.drag.originX + (dx * right.x - dy * forward.x) * mmPerPixel;
      state.drag.mesh.position.y = state.drag.originY + (dx * right.y - dy * forward.y) * mmPerPixel;
      return;
    }
    const next = state.drag.base.slice();
    next[state.drag.index] = state.drag.base[state.drag.index] + (state.drag.startX - event.clientX) * 0.15;
    state.poses[state.drag.file] = { ...(state.poses[state.drag.file] || {}), rotation: next };
    const geometry = geometryFromPositions(state.drag.positions, next, state.drag.mesh.userData.scale || 1);
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
  wrap.dataset.field = name;
  const title = document.createElement("span");
  title.textContent = label;
  if (extra.tip) {
    wrap.title = extra.tip;
    title.title = extra.tip;
  }
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
  wrap.append(title, input);
  return wrap;
}

function settingsSection(title, tip = "") {
  const section = document.createElement("section");
  section.className = "settings-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  if (tip) heading.title = tip;
  section.append(heading);
  if (tip) {
    const hint = document.createElement("p");
    hint.className = "settings-hint";
    hint.textContent = tip;
    section.append(hint);
  }
  const grid = document.createElement("div");
  grid.className = "grid";
  section.append(grid);
  return { section, grid };
}

function appendFields(host, specs) {
  for (const [name, label, type, extra] of specs) {
    host.append(field(name, label, type, extra || {}));
  }
}

function syncSettingsVisibility() {
  const form = $("settings-form");
  if (!form) return;
  const engine = form.querySelector('[name="slicer_engine"]')?.value || state.settings?.slicer_engine || "cura";
  const ironing = form.querySelector('[name="ironing_enabled"]')?.checked;
  const adhesion = form.querySelector('[name="adhesion_type"]')?.value || "none";
  const supports = form.querySelector('[name="support_enable"]')?.checked;
  const seamType = form.querySelector('[name="z_seam_type"]')?.value;
  const scarf = form.querySelector('[name="orca_scarf_joint"]')?.value;
  const show = (selector, visible) => {
    for (const node of form.querySelectorAll(selector)) node.hidden = !visible;
  };
  show('[data-cluster="ironing-options"]', Boolean(ironing));
  show('[data-cluster="cura-seam"]', engine === "cura");
  show('[data-cluster="cura-seam-position"]', engine === "cura" && seamType === "user_specified");
  show('[data-cluster="orca-seam"]', engine === "orca");
  show('[data-cluster="orca-scarf"]', engine === "orca");
  show('[data-cluster="orca-scarf-conditional"]', engine === "orca" && scarf && scarf !== "none");
  show('[data-cluster="orca-arrange"]', engine === "orca");
  show('[data-cluster="brim"]', adhesion === "brim");
  show('[data-cluster="skirt"]', adhesion === "skirt");
  show('[data-cluster="raft"]', adhesion === "raft");
  show('[data-cluster="supports"]', Boolean(supports));
}

function renderSettings() {
  const form = $("settings-form");
  const options = state.options;
  const engine = state.settings?.slicer_engine || "cura";
  form.innerHTML = "";

  const general = settingsSection("General", "Shared across printers. Switching engine changes which seam options are shown.");
  appendFields(general.grid, [
    ["slicer_engine", "Slicing engine", "select", { options: options.slicer_engines, labels: { cura: "Cura", orca: "Orca" }, tip: "CuraEngine or OrcaSlicer for every slice." }],
    ["output_folder_name", "Output folder name", "text", { tip: "Subfolder created under each printer in the output directory." }],
    ["rotation_x", "Shared rotation X", "number", { step: "1", tip: "Degrees applied to every part unless a part has its own rotation." }],
    ["rotation_y", "Shared rotation Y", "number", { step: "1", tip: "Degrees applied to every part unless a part has its own rotation." }],
    ["rotation_z", "Shared rotation Z", "number", { step: "1", tip: "Degrees applied to every part unless a part has its own rotation." }],
    ["layer_height", "Layer height (mm)", "number", { step: "0.01", tip: "Vertical resolution of the print." }],
  ]);
  form.append(general.section);

  const ironing = settingsSection("Ironing", "Smooths the top surface with a second pass.");
  const ironingEnable = field("ironing_enabled", "Ironing", "checkbox", { tip: "Enable the ironing pass on top surfaces." });
  const ironingOnly = field("ironing_only_highest_layer", "Ironing top layer only", "checkbox", { tip: "Limit ironing to the uppermost top surface." });
  ironingOnly.dataset.cluster = "ironing-options";
  ironingOnly.classList.add("cluster");
  ironing.grid.append(ironingEnable, ironingOnly);
  form.append(ironing.section);

  const seam = settingsSection("Seam", engine === "orca" ? "Orca seam placement and scarf joint options." : "Cura Z-seam type and corner position.");
  if (engine === "orca") {
    const seamField = field("orca_seam", "Seam", "select", {
      options: options.orca_seam_positions,
      labels: { nearest: "Nearest", aligned: "Aligned", aligned_back: "Aligned back", back: "Back", random: "Random" },
      tip: "Where Orca prefers to place the outer wall seam.",
    });
    seamField.dataset.cluster = "orca-seam";
    const scarf = field("orca_scarf_joint", "Scarf joint seam", "select", {
      options: options.orca_scarf_joints,
      labels: { none: "None", external: "Contour", all: "Contour and hole" },
      tip: "Use a scarf joint to hide the seam on outer contours, or on contours and holes.",
    });
    scarf.dataset.cluster = "orca-scarf";
    const conditional = field("orca_scarf_conditional", "Conditional scarf joint", "checkbox", {
      tip: "Apply scarf joints only on smooth perimeters where a normal seam would still show.",
    });
    conditional.dataset.cluster = "orca-scarf-conditional";
    conditional.classList.add("cluster");
    seam.grid.append(seamField, scarf, conditional);
  } else {
    const type = field("z_seam_type", "Seam", "select", { options: options.seam_types, tip: "How Cura chooses the Z seam." });
    type.dataset.cluster = "cura-seam";
    const position = field("z_seam_position", "Seam position", "select", { options: options.seam_positions, tip: "Corner used when seam is user specified." });
    position.dataset.cluster = "cura-seam-position";
    position.classList.add("cluster");
    seam.grid.append(type, position);
  }
  form.append(seam.section);

  const infill = settingsSection("Infill", "Interior fill pattern and density.");
  appendFields(infill.grid, [
    ["infill_pattern", "Infill", "select", { options: options.infill_patterns, tip: "Pattern used inside the model." }],
    ["infill_sparse_density", "Infill density (%)", "number", { step: "1", tip: "How solid the interior is." }],
    ["retraction_combing", "Combing", "select", { options: options.combing_modes, tip: "Keep travel moves inside the print to reduce stringing." }],
  ]);
  form.append(infill.section);

  const adhesion = settingsSection("Bed adhesion", "Extra material on the bed to help the print stick.");
  const adhesionType = field("adhesion_type", "Bed adhesion", "select", { options: options.adhesion_types, tip: "None, skirt, brim, or raft." });
  const brim = field("brim_width", "Brim width (mm)", "number", { step: "0.1", tip: "Width of the brim around the part." });
  brim.dataset.cluster = "brim";
  brim.classList.add("cluster");
  const skirt = field("skirt_line_count", "Skirt lines", "number", { step: "1", tip: "Number of skirt loops around the part." });
  skirt.dataset.cluster = "skirt";
  skirt.classList.add("cluster");
  const raft = field("raft_margin", "Raft margin (mm)", "number", { step: "0.1", tip: "How far the raft extends past the part." });
  raft.dataset.cluster = "raft";
  raft.classList.add("cluster");
  adhesion.grid.append(adhesionType, brim, skirt, raft);
  form.append(adhesion.section);

  const supports = settingsSection("Supports", "Scaffolding under overhangs.");
  const supportEnable = field("support_enable", "Supports", "checkbox", { tip: "Generate support structures." });
  const supportFields = document.createElement("div");
  supportFields.className = "grid cluster";
  supportFields.dataset.cluster = "supports";
  appendFields(supportFields, [
    ["support_type", "Support placement", "select", { options: options.support_types, tip: "Build plate only, or everywhere an overhang needs help." }],
    ["support_structure", "Support structure", "select", { options: options.support_structures, tip: "Normal grid supports or tree supports." }],
    ["support_angle", "Support angle", "number", { step: "1", tip: "Overhangs steeper than this angle get support." }],
    ["support_infill_rate", "Support density (%)", "number", { step: "1", tip: "Density of the support structure." }],
  ]);
  supports.grid.append(supportEnable);
  supports.section.append(supportFields);
  form.append(supports.section);

  const arrange = settingsSection(
    "Group arrangement",
    "Orca auto-arrange for groups. Parts stay separate objects; the arranged plate is saved as a .3mf beside the source files.",
  );
  arrange.section.dataset.cluster = "orca-arrange";
  arrange.section.classList.add("cluster");
  appendFields(arrange.grid, [
    ["orca_arrange_spacing", "Spacing (0 = Auto)", "number", { step: "0.1", tip: "Minimum gap between parts. 0 uses Orca Auto spacing (brim-aware)." }],
    ["orca_arrange_rotate", "Auto rotate for arrangement", "checkbox", { tip: "Let Orca rotate parts on the bed while packing." }],
    ["orca_arrange_multicolor", "Allow multiple materials on same plate", "checkbox", { tip: "Keep differently coloured parts on one plate when arranging." }],
    ["orca_arrange_align_y", "Align to Y axis", "checkbox", { tip: "Prefer aligning parts along Y to reduce bed motion on i3-style printers." }],
  ]);
  form.append(arrange.section);

  for (const input of form.querySelectorAll("input, select")) {
    const value = state.settings[input.name];
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
    input.addEventListener("change", async () => {
      syncSettingsVisibility();
      await saveSettings();
    });
  }
  syncSettingsVisibility();
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
    state.orcaCatalog = false;
    $("orca-catalog").checked = false;
    await loadPrinters();
    renderFilaments();
    renderOwnedForm();
    renderSettings();
  }
  if ($("models").classList.contains("active")) await showLayout();
}

async function loadPrinters() {
  const catalog = state.settings?.slicer_engine === "orca" && state.orcaCatalog;
  state.printers = await api(`/api/printers${catalog ? "?catalog=true" : ""}`);
  renderPrinters();
}

function renderPrinters() {
  const notice = $("printer-notice");
  const engine = state.settings?.slicer_engine || "cura";
  const root = engine === "orca" ? state.health?.orca_config_dir : state.health?.config_dir;
  $("show-all-orca").hidden = engine !== "orca";
  const ownedIds = new Set((state.owned?.printers || []).map((item) => item.orca_id).filter(Boolean));
  if (!state.printers.length) {
    notice.textContent = engine === "orca"
      ? "No Orca profiles are linked to your printers yet. Show every Orca printer to add the ones you own."
      : root
        ? `No printers in ${root}. Cura keeps machine profiles in a version folder such as %APPDATA%\\cura\\5.13 (the folder that contains machine_instances).`
        : "No printers found.";
  } else if (engine === "orca" && !state.orcaCatalog) {
    notice.textContent = `Showing the ${state.printers.length} Orca profiles linked to your printers${root ? ` from ${root}` : ""}.`;
  } else if (engine === "orca") {
    notice.textContent = `Showing every Orca printer${root ? ` from ${root}` : ""}. Add a machine to keep it in the default list.`;
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
    const canAdd = engine === "orca" && state.orcaCatalog && !ownedIds.has(printer.id);
    row.innerHTML = `<td><input type="checkbox" ${printer.enabled ? "checked" : ""} ${printer.slicable ? "" : "disabled"}></td>
      <td>${printer.name}</td>
      <td>${printer.machine_width ?? "?"} × ${printer.machine_depth ?? "?"} × ${printer.machine_height ?? "?"}</td>
      <td class="${printer.error ? "" : "muted"}">${notes}</td>
      <td>${canAdd ? `<button type="button">Add</button>` : `<a href="/api/bundles?printer_id=${encodeURIComponent(printer.id)}">Download</a>`}</td>`;
    if (canAdd) {
      row.querySelector("button").addEventListener("click", async () => {
        await api("/api/owned", { method: "POST", body: JSON.stringify({ orca_id: printer.id }) });
        state.owned = await api("/api/owned");
        renderOwned();
        await loadPrinters();
      });
    }
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

async function saveIncluded(paths) {
  state.locations = await api("/api/locations", {
    method: "PUT",
    body: JSON.stringify({ included: paths }),
  });
  state.models = await api("/api/models");
  renderModels();
}

async function saveGroups(groups) {
  state.groups = (await api("/api/groups", { method: "PUT", body: JSON.stringify({ groups }) })).groups;
  renderModels();
  if ($("models").classList.contains("active")) await showLayout();
}

async function deleteGroup(groupId) {
  const group = state.groups.find((item) => item.id === groupId);
  if (!group) return;
  const label = group.name || "this group";
  if (!window.confirm(`Delete group “${label}”? Arranged .3mf files on disk are kept.`)) return;
  const next = state.groups.filter((item) => item.id !== groupId);
  await saveGroups(next);
}

function renderGroupChips(host) {
  if (!state.groups.length) return;
  const chips = document.createElement("div");
  chips.className = "group-chip-list";
  for (const group of state.groups) {
    const chip = document.createElement("span");
    chip.className = "group-chip";
    const name = document.createElement("span");
    name.textContent = `${group.name} (${group.files.length})`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = `Delete group ${group.name}`;
    remove.addEventListener("click", () => deleteGroup(group.id));
    chip.append(name, remove);
    chips.append(chip);
  }
  host.append(chips);
}

function renderModels() {
  const list = $("model-list");
  list.innerHTML = "";
  const groupSelect = $("preview-group");
  const previousGroup = groupSelect.value;
  groupSelect.innerHTML = "";
  for (const group of state.groups) {
    const option = document.createElement("option");
    option.value = group.id;
    option.textContent = group.name;
    groupSelect.append(option);
  }
  if ([...groupSelect.options].some((option) => option.value === previousGroup)) {
    groupSelect.value = previousGroup;
  }

  if (!state.models.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No STL or 3MF files in this folder. Browse to a folder that contains models, or paste its path above.";
    list.append(empty);
    renderGroupChips(list);
    return;
  }

  if (!state.selectedModel || !state.models.some((model) => model.path === state.selectedModel)) {
    state.selectedModel = state.models[0].path;
  }

  const table = document.createElement("table");
  table.className = "model-matrix";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");

  const selectAllTh = document.createElement("th");
  selectAllTh.className = "col-include";
  const selectAllLabel = document.createElement("label");
  selectAllLabel.className = "inline select-all";
  selectAllLabel.title = "Include all files for individual slicing";
  const selectAll = document.createElement("input");
  selectAll.type = "checkbox";
  const includedCount = state.models.filter((model) => model.included).length;
  selectAll.checked = includedCount > 0 && includedCount === state.models.length;
  selectAll.indeterminate = includedCount > 0 && includedCount < state.models.length;
  selectAll.addEventListener("change", async () => {
    const paths = selectAll.checked ? state.models.map((model) => model.path) : [];
    await saveIncluded(paths);
  });
  selectAllLabel.append(selectAll, document.createTextNode(" All"));
  selectAllTh.append(selectAllLabel);
  headRow.append(selectAllTh);

  const fileTh = document.createElement("th");
  fileTh.className = "col-file";
  fileTh.textContent = "File";
  headRow.append(fileTh);

  for (const group of state.groups) {
    const th = document.createElement("th");
    th.className = "col-group";
    th.title = group.name;
    const wrap = document.createElement("div");
    wrap.className = "group-head";
    const title = document.createElement("span");
    title.className = "group-head-title";
    title.textContent = group.name;
    title.title = group.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "group-remove";
    remove.textContent = "×";
    remove.title = `Delete group ${group.name}`;
    remove.addEventListener("click", async (event) => {
      event.stopPropagation();
      await deleteGroup(group.id);
    });
    wrap.append(title, remove);
    th.append(wrap);
    headRow.append(th);
  }
  thead.append(headRow);
  table.append(thead);

  const tbody = document.createElement("tbody");
  for (const model of state.models) {
    const row = document.createElement("tr");
    if (model.path === state.selectedModel) row.classList.add("selected");

    const includeTd = document.createElement("td");
    includeTd.className = "col-include";
    const include = document.createElement("input");
    include.type = "checkbox";
    include.checked = Boolean(model.included);
    include.title = "Include for individual slicing";
    include.addEventListener("click", (event) => event.stopPropagation());
    include.addEventListener("change", async () => {
      const paths = state.models
        .filter((item) => (item.path === model.path ? include.checked : item.included))
        .map((item) => item.path);
      await saveIncluded(paths);
    });
    includeTd.append(include);
    row.append(includeTd);

    const fileTd = document.createElement("td");
    fileTd.className = "col-file";
    const fileButton = document.createElement("button");
    fileButton.type = "button";
    fileButton.className = "file-pick";
    fileButton.textContent = model.path;
    fileButton.title = model.path;
    fileButton.addEventListener("click", async () => {
      state.selectedModel = model.path;
      $("preview-kind").value = "model";
      renderModels();
      await showLayout();
    });
    fileTd.append(fileButton);
    row.append(fileTd);

    for (const group of state.groups) {
      const cell = document.createElement("td");
      cell.className = "col-group";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = group.files.includes(model.path);
      box.title = `Include in ${group.name}`;
      box.addEventListener("click", (event) => event.stopPropagation());
      box.addEventListener("change", async () => {
        const next = state.groups.map((item) => {
          if (item.id !== group.id) return item;
          const files = box.checked
            ? [...new Set([...item.files, model.path])]
            : item.files.filter((file) => file !== model.path);
          return { ...item, files, manual_layout: null, arranged_3mf: null };
        });
        await saveGroups(next);
      });
      cell.append(box);
      row.append(cell);
    }

    row.addEventListener("click", async (event) => {
      if (event.target.closest("input, button")) return;
      state.selectedModel = model.path;
      $("preview-kind").value = "model";
      renderModels();
      await showLayout();
    });
    tbody.append(row);
  }
  table.append(tbody);
  list.append(table);
  renderGroupChips(list);
}

function jobItemLabel(item) {
  return `${item.printer_name} · ${item.item_name}`;
}

function bedSizeForPrinter(printerId) {
  if (!printerId) return null;
  const owned = (state.owned?.printers || []).find(
    (item) => item.orca_id === printerId || item.cura_id === printerId || item.id === printerId,
  );
  const settings = owned && machineSettingsForOwned(owned);
  if (settings?.bed_width && settings?.bed_depth) {
    return { width: Number(settings.bed_width), depth: Number(settings.bed_depth) };
  }
  const printer = (state.printers || []).find((item) => item.id === printerId);
  if (printer?.machine_width && printer?.machine_depth) {
    return { width: Number(printer.machine_width), depth: Number(printer.machine_depth) };
  }
  return null;
}

function machineSettingsForOwned(owned) {
  const engine = state.settings?.slicer_engine === "orca" ? "orca" : "cura";
  const custom = engine === "orca" ? owned.orca_settings : owned.cura_settings;
  return custom || owned.settings || null;
}

function setGcodeBed(width, depth) {
  if (!width || !depth || width <= 0 || depth <= 0) {
    state.gcodeBed = null;
    gcodeBed.visible = false;
    gcodeBed.geometry.dispose();
    gcodeBed.geometry = new BufferGeometry();
    return;
  }
  state.gcodeBed = { width, depth };
  gcodeBed.visible = true;
  const points = [
    new Vector3(0, 0, 0),
    new Vector3(width, 0, 0),
    new Vector3(width, depth, 0),
    new Vector3(0, depth, 0),
  ];
  gcodeBed.geometry.dispose();
  gcodeBed.geometry = new BufferGeometry().setFromPoints(points);
}

function updateGcodeSelectionLabel() {
  const label = $("gcode-selection");
  if (!label) return;
  if (!state.gcodeLabel) {
    label.textContent = "Select a finished job row to preview its gcode.";
    return;
  }
  const bed = state.gcodeBed;
  const bedText = bed ? ` · bed ${formatMm(bed.width)}×${formatMm(bed.depth)} mm` : "";
  label.textContent = `Showing ${state.gcodeLabel}${bedText}`;
}

function formatMm(value) {
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 10) / 10);
}

function selectJobRow(body, row) {
  for (const other of body.querySelectorAll("tr")) other.classList.remove("selected");
  row.classList.add("selected");
}

function renderJob(job) {
  $("job-status").textContent = job && job.status ? `${job.kind} ${job.status}` : "No job yet";
  const body = $("job-rows");
  body.innerHTML = "";
  const errorBox = $("job-error");
  const failed = (job?.items || []).filter((item) => item.error);
  if (failed.length) {
    errorBox.hidden = false;
    errorBox.textContent = failed.map((item) => `${item.printer_name} · ${item.item_name}\n${item.error}`).join("\n\n");
  } else {
    errorBox.hidden = true;
    errorBox.textContent = "";
  }
  for (const item of job?.items || []) {
    const row = document.createElement("tr");
    if (item.error || item.status === "failed") row.classList.add("job-failed");
    const viewable = item.status === "done" && item.output_path;
    if (viewable || item.error) row.classList.add("job-selectable");
    if (viewable && state.gcodePath === item.output_path) row.classList.add("selected");
    const time = item.time_seconds == null ? "" : formatDuration(item.time_seconds);
    const filament = item.filament_meters == null ? "" : `${item.filament_meters.toFixed(2)} m`;
    const status = item.progress && item.progress !== item.status
      ? `${item.status} — ${item.progress}`
      : item.status;
    row.innerHTML = `<td>${item.printer_name}</td><td>${item.item_type}: ${item.item_name}</td><td class="job-status">${status}</td><td>${time}</td><td>${filament}</td><td></td>`;
    if (item.error) {
      row.title = "Click to show the full error below the table";
      row.addEventListener("click", (event) => {
        if (event.target.closest("a, button")) return;
        selectJobRow(body, row);
        errorBox.hidden = false;
        errorBox.textContent = `${item.printer_name} · ${item.item_name}\n${item.error}`;
        errorBox.scrollIntoView({ block: "nearest" });
      });
    }
    if (viewable) {
      row.title = "Click to preview gcode";
      const link = document.createElement("a");
      link.href = `/api/output?path=${encodeURIComponent(item.output_path)}`;
      link.textContent = "Download";
      row.lastElementChild.append(link);
      const view = document.createElement("button");
      view.type = "button";
      view.textContent = "View";
      const openPreview = () => {
        selectJobRow(body, row);
        loadGcode(item.output_path, jobItemLabel(item), bedSizeForPrinter(item.printer_id));
      };
      view.addEventListener("click", (event) => {
        event.stopPropagation();
        openPreview();
      });
      row.lastElementChild.append(view);
      row.addEventListener("click", (event) => {
        if (event.target.closest("a, button")) return;
        openPreview();
      });
    }
    body.append(row);
  }
  updateGcodeSelectionLabel();
  const testItem = (job?.items || []).find(
    (item) => job.kind === "test" && item.status === "done" && item.output_path,
  );
  if (testItem && state.gcodePath !== testItem.output_path) {
    loadGcode(testItem.output_path, jobItemLabel(testItem), bedSizeForPrinter(testItem.printer_id));
  }
}

function formatDuration(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function frameGcodeCamera(min, max) {
  const sizeX = Math.max(1, max.x - min.x);
  const sizeY = Math.max(1, max.y - min.y);
  const sizeZ = Math.max(1, max.z - min.z);
  gcodeView.orbit.target.set((min.x + max.x) / 2, (min.y + max.y) / 2, (min.z + max.z) / 2);
  gcodeView.orbit.radius = Math.max(sizeX, sizeY, sizeZ) * 1.8;
  gcodeView.orbit.theta = 0.7;
  gcodeView.orbit.phi = 1.05;
  state.gcodeFramed = true;
}

function gcodeFrameBounds(toolMin, toolMax) {
  const min = { ...toolMin };
  const max = { ...toolMax };
  const bed = state.gcodeBed;
  if (bed) {
    min.x = Math.min(min.x, 0);
    min.y = Math.min(min.y, 0);
    min.z = Math.min(min.z, 0);
    max.x = Math.max(max.x, bed.width);
    max.y = Math.max(max.y, bed.depth);
    max.z = Math.max(max.z, 0);
  }
  return { min, max };
}

async function loadGcode(path, label = null, bed = null) {
  state.gcodePath = path;
  state.gcodeLabel = label || path;
  state.gcodeFramed = false;
  state.gcodeDrag = null;
  if (bed) setGcodeBed(bed.width, bed.depth);
  else setGcodeBed(0, 0);
  updateGcodeSelectionLabel();
  const info = await api(`/api/gcode/info?path=${encodeURIComponent(path)}`);
  state.gcodeLayers = info.layer_count;
  const slider = $("layer-slider");
  slider.max = String(Math.max(0, info.layer_count - 1));
  slider.value = "0";
  await drawLayer(0, true);
}

async function drawLayer(index, frame = false) {
  if (!state.gcodePath) return;
  const request = ++state.gcodeLayerRequest;
  $("layer-label").textContent = `${index + 1} / ${state.gcodeLayers}`;
  const include = $("show-travel").checked;
  const path = state.gcodePath;
  const layer = await api(`/api/gcode/layer?path=${encodeURIComponent(path)}&index=${index}&include_travel=${include}`);
  if (request !== state.gcodeLayerRequest || state.gcodePath !== path) return;
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
  const min = { x: Infinity, y: Infinity, z: Infinity };
  const max = { x: -Infinity, y: -Infinity, z: -Infinity };
  for (const polyline of layer.polylines) {
    const points = polyline.points.map((point) => new Vector3(point[0], point[1], point[2]));
    for (const point of points) {
      min.x = Math.min(min.x, point.x);
      min.y = Math.min(min.y, point.y);
      min.z = Math.min(min.z, point.z);
      max.x = Math.max(max.x, point.x);
      max.y = Math.max(max.y, point.y);
      max.z = Math.max(max.z, point.z);
    }
    const line = new Line(
      new BufferGeometry().setFromPoints(points),
      new LineBasicMaterial({ color: new Color(colors[polyline.type] || 0xffffff) }),
    );
    line.userData.toolpath = true;
    gcodeView.scene.add(line);
  }
  if ((frame || !state.gcodeFramed) && (Number.isFinite(min.x) || state.gcodeBed)) {
    const bounds = Number.isFinite(min.x)
      ? gcodeFrameBounds(min, max)
      : gcodeFrameBounds(
          { x: 0, y: 0, z: 0 },
          { x: state.gcodeBed.width, y: state.gcodeBed.depth, z: 0 },
        );
    frameGcodeCamera(bounds.min, bounds.max);
  }
}

function bindGcodePointer() {
  const canvas = gcodeView.canvas;
  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    state.gcodeDrag = {
      x: event.clientX,
      y: event.clientY,
      theta: gcodeView.orbit.theta,
      phi: gcodeView.orbit.phi,
    };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!state.gcodeDrag) return;
    gcodeView.orbit.theta = state.gcodeDrag.theta - (event.clientX - state.gcodeDrag.x) * 0.01;
    gcodeView.orbit.phi = Math.min(
      1.45,
      Math.max(0.15, state.gcodeDrag.phi + (event.clientY - state.gcodeDrag.y) * 0.01),
    );
  });
  const endDrag = () => {
    state.gcodeDrag = null;
  };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("lostpointercapture", endDrag);
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    gcodeView.orbit.radius = Math.max(20, Math.min(5000, gcodeView.orbit.radius * (event.deltaY > 0 ? 1.08 : 0.92)));
  }, { passive: false });
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

const TEMP_FIELDS = [
  "nozzle_temperature",
  "nozzle_temperature_initial",
  "bed_temperature",
  "bed_temperature_initial",
];

const OWNED_SECTIONS = [
  ["Dimensions", [
    ["bed_width", "Bed width (mm)", "number", "0.1"],
    ["bed_depth", "Bed depth (mm)", "number", "0.1"],
    ["bed_height", "Bed height (mm)", "number", "0.1"],
    ["nozzle_diameter", "Nozzle diameter (mm)", "number", "0.1"],
  ]],
  ["Machine configuration", [
    ["heated_bed", "Heated bed", "checkbox"],
    ["gcode_flavor", "G-code flavor", "select"],
  ]],
  ["Temperatures", [
    ["temperature_override", "Use this printer's temperatures", "checkbox"],
    ["nozzle_temperature", "Nozzle temperature (°C)", "number", "1"],
    ["nozzle_temperature_initial", "Initial nozzle temperature (°C)", "number", "1"],
    ["bed_temperature", "Bed temperature (°C)", "number", "1"],
    ["bed_temperature_initial", "Initial bed temperature (°C)", "number", "1"],
  ]],
  ["Speeds", [
    ["retraction_length", "Retraction length (mm)", "number", "0.1"],
    ["retraction_speed", "Retraction speed (mm/s)", "number", "1"],
    ["travel_speed", "Travel speed (mm/s)", "number", "1"],
  ]],
  ["Z hop", [
    ["z_hop", "Z hop height (mm)", "number", "0.1"],
    ["z_hop_type", "Z hop type", "select"],
  ]],
];

function selectedOwned() {
  return (state.owned?.printers || []).find((item) => item.id === $("owned-printer").value) || null;
}

function renderOwned() {
  const select = $("owned-printer");
  const previous = select.value;
  select.innerHTML = "";
  for (const printer of state.owned?.printers || []) {
    const option = document.createElement("option");
    option.value = printer.id;
    option.textContent = printer.name;
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  renderOwnedForm();
}

function selectedFilament() {
  return (state.filaments?.filaments || []).find((item) => item.id === state.filaments.selected_id) || null;
}

function renderOwnedForm() {
  const form = $("owned-form");
  const printer = selectedOwned();
  const notice = $("owned-notice");
  form.innerHTML = "";
  renderFilaments();
  if (!printer) {
    notice.textContent = "No printers are available yet.";
    return;
  }
  notice.textContent = ownedNotice(printer);
  const settings = displayedSettings(printer);
  state.tempDraft = null;
  for (const [title, fields] of OWNED_SECTIONS) {
    const section = document.createElement("fieldset");
    section.className = "settings-section";
    const legend = document.createElement("legend");
    legend.textContent = title;
    section.append(legend);
    const grid = document.createElement("div");
    grid.className = "grid";
    if (title === "Machine configuration") grid.append(orcaProfileField(printer));
    for (const spec of fields) grid.append(ownedField(spec, settings));
    if (title === "Temperatures") {
      const hint = document.createElement("p");
      hint.id = "temperature-note";
      hint.className = "muted span-2";
      grid.append(hint);
    }
    section.append(grid);
    form.append(section);
  }
  const scripts = document.createElement("fieldset");
  scripts.className = "settings-section";
  const legend = document.createElement("legend");
  legend.textContent = "Custom G-code";
  scripts.append(legend);
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const [name, label] of [
    ["cura_start_gcode", "Cura start G-code"],
    ["cura_end_gcode", "Cura end G-code"],
    ["orca_start_gcode", "Orca start G-code"],
    ["orca_end_gcode", "Orca end G-code"],
  ]) {
    const wrap = document.createElement("label");
    wrap.className = "span-2";
    wrap.textContent = label;
    const input = document.createElement("textarea");
    input.name = name;
    input.value = settings[name] || "";
    wrap.append(input);
    grid.append(wrap);
  }
  const copy = document.createElement("div");
  copy.className = "toolbar span-2";
  copy.innerHTML = `<button type="button" id="copy-scripts-to-orca">Copy Cura scripts to Orca</button>
    <button type="button" id="copy-scripts-to-cura">Copy Orca scripts to Cura</button>`;
  grid.append(copy);
  scripts.append(grid);
  form.append(scripts);
  form.querySelector("#copy-scripts-to-orca").addEventListener("click", () => {
    form.elements.orca_start_gcode.value = form.elements.cura_start_gcode.value;
    form.elements.orca_end_gcode.value = form.elements.cura_end_gcode.value;
  });
  form.querySelector("#copy-scripts-to-cura").addEventListener("click", () => {
    form.elements.cura_start_gcode.value = form.elements.orca_start_gcode.value;
    form.elements.cura_end_gcode.value = form.elements.orca_end_gcode.value;
  });
  form.elements.temperature_override.addEventListener("change", () => syncTemperatureFields(true));
  syncTemperatureFields(false);
}

function orcaProfileField(printer) {
  const link = document.createElement("label");
  link.textContent = "Orca profile";
  const orca = document.createElement("select");
  orca.name = "orca_id";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "None";
  orca.append(empty);
  for (const profile of state.owned.orca_profiles || []) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.name;
    orca.append(option);
  }
  orca.value = printer.orca_id || "";
  link.append(orca);
  return link;
}

function ownedField([name, label, type, step], settings) {
  const wrap = document.createElement("label");
  wrap.textContent = label;
  const input = document.createElement(type === "select" ? "select" : "input");
  input.name = name;
  if (type === "checkbox") {
    input.type = "checkbox";
    input.checked = Boolean(settings[name]);
    wrap.classList.add("inline");
  } else if (type === "select") {
    const options = selectOptionsForField(name);
    for (const value of options) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      input.append(option);
    }
    input.value = settings[name] || options[0] || "";
  } else {
    input.type = "number";
    input.step = step;
    input.value = settings[name] ?? "";
  }
  wrap.append(input);
  if (name === "gcode_flavor") {
    const hint = document.createElement("span");
    hint.className = "muted";
    hint.textContent = "Orca writes Klipper. Cura has no Klipper flavor, so a Cura slice uses Marlin gcode, which Klipper runs.";
    wrap.append(hint);
  }
  if (name === "z_hop") {
    const hint = document.createElement("span");
    hint.className = "muted";
    hint.textContent = "0 disables Z hop. Orca also gets the hop type below; Cura enables retraction hop when height is above 0.";
    wrap.append(hint);
  }
  if (name === "z_hop_type") {
    const hint = document.createElement("span");
    hint.className = "muted";
    hint.textContent = "Used by Orca (Normal / Slope / Spiral / Auto Lift).";
    wrap.append(hint);
  }
  if (name === "temperature_override") {
    const hint = document.createElement("span");
    hint.className = "muted";
    hint.textContent = state.settings?.slicer_engine === "orca"
      ? "Off uses the filament selected above. On keeps the temperatures saved for this printer."
      : "Off uses the shared filament. That list is edited while Orca is the slicing engine.";
    wrap.append(hint);
  }
  return wrap;
}

function selectOptionsForField(name) {
  if (name === "z_hop_type") return state.options.z_hop_types || ["Normal Lift"];
  if (name === "gcode_flavor") return state.options.gcode_flavors || ["Marlin"];
  return [];
}

function syncTemperatureFields(fromToggle) {
  const form = $("owned-form");
  const printer = selectedOwned();
  if (!form.elements.temperature_override || !printer) return;
  const override = form.elements.temperature_override.checked;
  const filament = selectedFilament();
  const follow = !override && Boolean(filament);
  if (fromToggle && !override) state.tempDraft = readTemperatures(form);
  const source = follow ? filament : (state.tempDraft || printer.settings || {});
  for (const name of TEMP_FIELDS) {
    const input = form.elements[name];
    input.disabled = follow;
    if (follow || (fromToggle && override)) input.value = source[name] ?? "";
  }
  const note = $("temperature-note");
  if (note) note.textContent = temperatureNote(override, filament, source);
}

function readTemperatures(form) {
  const values = {};
  for (const name of TEMP_FIELDS) values[name] = Number(form.elements[name].value);
  return values;
}

function temperatureNote(override, filament, source) {
  const nozzle = source.nozzle_temperature ?? "";
  const nozzleInitial = source.nozzle_temperature_initial ?? "";
  const bed = source.bed_temperature ?? "";
  const bedInitial = source.bed_temperature_initial ?? "";
  const inherits = filament?.orca_name || "fdm_filament_pla";
  const filamentName = filament?.name || "Generic PLA";
  const using = !override && filament
    ? `Using ${filamentName} for this printer.`
    : "Using this printer's temperatures.";
  const orca = state.settings?.slicer_engine === "orca"
    ? ` Orca writes them on filament “${filamentName}” (inherits ${inherits}): nozzle_temperature ${nozzle}, nozzle_temperature_initial_layer ${nozzleInitial}, and cool_plate_temp, eng_plate_temp, hot_plate_temp, textured_plate_temp ${bed}, with each plate's initial layer at ${bedInitial}.`
    : "";
  const cura = ` Cura writes material_print_temperature ${nozzle}, material_print_temperature_layer_0 ${nozzleInitial}, material_bed_temperature ${bed}, and material_bed_temperature_layer_0 ${bedInitial}.`;
  return using + orca + cura;
}

function renderFilaments() {
  const panel = $("filament-panel");
  const orca = state.settings?.slicer_engine === "orca";
  panel.hidden = !orca;
  if (!orca) return;
  const select = $("filament-selected");
  const previous = select.value;
  select.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "None";
  select.append(none);
  for (const filament of state.filaments?.filaments || []) {
    const option = document.createElement("option");
    option.value = filament.id;
    option.textContent = filament.name;
    select.append(option);
  }
  const wanted = state.filaments?.selected_id || previous;
  if ([...select.options].some((option) => option.value === wanted)) select.value = wanted;
  const list = $("filament-list");
  list.innerHTML = "";
  const filaments = state.filaments?.filaments || [];
  $("filament-notice").textContent = filaments.length
    ? "Select a row to edit its temperatures. Adding a filament selects it for every printer when none is selected yet."
    : "Search the Orca catalog and add the filaments you print with.";
  for (const filament of filaments) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${filament.name} — nozzle ${filament.nozzle_temperature}° / ${filament.nozzle_temperature_initial}° · bed ${filament.bed_temperature}° / ${filament.bed_temperature_initial}°`;
    if (filament.id === state.filamentEdit) button.classList.add("primary");
    button.addEventListener("click", () => {
      state.filamentEdit = filament.id;
      renderFilamentForm();
      renderFilaments();
    });
    list.append(button);
  }
  renderFilamentForm();
  fillCatalogSelect();
}

function renderFilamentForm() {
  const form = $("filament-form");
  const filament = (state.filaments?.filaments || []).find((item) => item.id === state.filamentEdit);
  form.innerHTML = "";
  if (!filament) return;
  const fields = [
    ["name", "Name", "text"],
    ["nozzle_temperature", "Nozzle temperature (°C)", "number"],
    ["nozzle_temperature_initial", "Initial nozzle temperature (°C)", "number"],
    ["bed_temperature", "Bed temperature (°C)", "number"],
    ["bed_temperature_initial", "Initial bed temperature (°C)", "number"],
  ];
  for (const [name, label, type] of fields) {
    const wrap = document.createElement("label");
    wrap.textContent = label;
    const input = document.createElement("input");
    input.name = name;
    input.type = type;
    if (type === "number") input.step = "1";
    input.value = filament[name] ?? "";
    wrap.append(input);
    form.append(wrap);
  }
  const actions = document.createElement("div");
  actions.className = "toolbar span-2";
  actions.innerHTML = `<button type="button" id="save-filament" class="primary">Save filament</button>
    <button type="button" id="delete-filament">Remove</button>`;
  form.append(actions);
  const keys = document.createElement("p");
  keys.className = "muted span-2";
  const inherits = filament.orca_name || "fdm_filament_pla";
  keys.textContent = `Saved temperatures override ${inherits}: nozzle_temperature, nozzle_temperature_initial_layer, and the cool, engineering, hot, and textured plate keys, including each initial-layer key.`;
  form.append(keys);
  form.querySelector("#save-filament").addEventListener("click", saveFilament);
  form.querySelector("#delete-filament").addEventListener("click", deleteFilament);
}

function fillCatalogSelect() {
  const select = $("filament-catalog");
  const previous = select.value;
  select.innerHTML = "";
  if (!state.filamentCatalog.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = state.filamentSearch === "ok" ? "No matching filaments" : "Search to list filaments";
    select.append(option);
    return;
  }
  for (const filament of state.filamentCatalog) {
    const option = document.createElement("option");
    option.value = filament.id;
    option.textContent = `${filament.vendor ? `${filament.vendor} · ` : ""}${filament.name} — ${filament.bed_plate} ${filament.bed_temperature}° / nozzle ${filament.nozzle_temperature}°`;
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}

async function saveFilament() {
  const filament = (state.filaments?.filaments || []).find((item) => item.id === state.filamentEdit);
  if (!filament) return;
  const form = $("filament-form");
  const payload = { ...filament };
  for (const input of form.querySelectorAll("input")) {
    payload[input.name] = input.type === "number" ? Number(input.value) : input.value;
  }
  const saved = await api("/api/filaments", { method: "PUT", body: JSON.stringify(payload) });
  state.filaments = await api("/api/filaments");
  state.filamentEdit = saved.id;
  renderFilaments();
  syncTemperatureFields(false);
}

async function deleteFilament() {
  if (!state.filamentEdit) return;
  state.filaments = await api(`/api/filaments?filament_id=${encodeURIComponent(state.filamentEdit)}`, { method: "DELETE" });
  state.filamentEdit = state.filaments.filaments[0]?.id || null;
  renderFilaments();
  syncTemperatureFields(false);
}

function displayedSettings(printer) {
  if (state.settings?.slicer_engine === "orca" && printer.orca_settings) return printer.orca_settings;
  if (state.settings?.slicer_engine !== "orca" && printer.cura_settings) return printer.cura_settings;
  return printer.settings || {};
}

function ownedNotice(printer, extra = "") {
  const cura = printer.cura_settings ? "Cura uses its own saved settings" : "Cura uses the shared settings";
  const orca = printer.orca_settings ? "Orca uses its own saved settings" : "Orca uses the shared settings";
  const base = `Cura profile: ${printer.cura_name || "none"} · Orca profile: ${printer.orca_name || "none"}. ${cura}. ${orca}.`;
  return extra ? `${extra} ${base}` : base;
}

function ownedPayload() {
  const printer = selectedOwned();
  const form = $("owned-form");
  const settings = { ...(displayedSettings(printer)) };
  for (const input of form.querySelectorAll("input, select, textarea")) {
    if (input.name === "orca_id" || input.disabled) continue;
    settings[input.name] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
  }
  if (!settings.temperature_override && state.tempDraft) {
    Object.assign(settings, state.tempDraft);
  }
  return {
    ...printer,
    orca_id: form.elements.orca_id.value || null,
    settings,
  };
}

$("owned-printer").addEventListener("change", renderOwnedForm);
$("save-cura").addEventListener("click", () => saveOwned("cura"));
$("save-orca").addEventListener("click", () => saveOwned("orca"));
$("save-owned").addEventListener("click", () => saveOwned("both"));

async function saveOwned(scope) {
  const saved = await api("/api/owned", { method: "PUT", body: JSON.stringify({ ...ownedPayload(), scope }) });
  state.owned = await api("/api/owned");
  $("owned-printer").value = saved.id;
  if (scope === "both") {
    renderOwned();
  } else {
    const printer = selectedOwned();
    const label = scope === "cura" ? "Saved for Cura." : "Saved for Orca.";
    if (printer) $("owned-notice").textContent = ownedNotice(printer, label);
  }
  if (state.settings?.slicer_engine === "orca") await loadPrinters();
}

$("pull-cura").addEventListener("click", async () => {
  const printer = selectedOwned();
  if (!printer) return;
  const pulled = await api("/api/owned/pull", { method: "POST", body: JSON.stringify({ id: printer.id, engine: "cura" }) });
  printer.settings = pulled.settings;
  if (printer.cura_settings && state.settings?.slicer_engine !== "orca") printer.cura_settings = pulled.settings;
  renderOwnedForm();
});
$("pull-orca").addEventListener("click", async () => {
  const printer = selectedOwned();
  if (!printer) return;
  const pulled = await api("/api/owned/pull", { method: "POST", body: JSON.stringify({ id: printer.id, engine: "orca" }) });
  printer.settings = pulled.settings;
  if (printer.orca_settings && state.settings?.slicer_engine === "orca") printer.orca_settings = pulled.settings;
  renderOwnedForm();
});

$("filament-selected").addEventListener("change", async () => {
  state.filaments = await api("/api/filaments/selected", {
    method: "PUT",
    body: JSON.stringify({ id: $("filament-selected").value }),
  });
  syncTemperatureFields(false);
  renderFilaments();
});

let filamentSearchTimer = 0;
let filamentSearchRequest = 0;

function setFilamentSearchStatus(status, detail = "") {
  state.filamentSearch = status;
  const node = $("filament-search-status");
  node.className = `search-status${status === "idle" ? "" : ` ${status}`}`;
  node.textContent = status === "ok" ? "✓" : status === "error" ? "×" : "";
  node.title = detail;
  node.setAttribute("aria-label", detail);
}

$("filament-search").addEventListener("input", () => {
  clearTimeout(filamentSearchTimer);
  const query = $("filament-search").value.trim();
  const request = ++filamentSearchRequest;
  if (query.length < 2) {
    state.filamentCatalog = [];
    setFilamentSearchStatus("idle", "");
    fillCatalogSelect();
    return;
  }
  setFilamentSearchStatus("loading", "Searching filaments");
  filamentSearchTimer = setTimeout(async () => {
    try {
      const found = await api(`/api/filaments/catalog?q=${encodeURIComponent(query)}`);
      if (request !== filamentSearchRequest) return;
      state.filamentCatalog = found;
      const label = found.length === 1 ? "1 filament" : `${found.length} filaments`;
      setFilamentSearchStatus("ok", label);
      fillCatalogSelect();
    } catch (error) {
      if (request !== filamentSearchRequest) return;
      state.filamentCatalog = [];
      setFilamentSearchStatus("error", error.message || "Search failed");
      fillCatalogSelect();
    }
  }, 250);
});

$("filament-add").addEventListener("click", async () => {
  const orcaId = $("filament-catalog").value;
  if (!orcaId) return;
  const saved = await api("/api/filaments", { method: "POST", body: JSON.stringify({ orca_id: orcaId }) });
  state.filaments = await api("/api/filaments");
  state.filamentEdit = saved.id;
  renderFilaments();
  syncTemperatureFields(false);
});

$("orca-catalog").addEventListener("change", async (event) => {
  state.orcaCatalog = event.target.checked;
  await loadPrinters();
});

$("refresh-printers").addEventListener("click", async () => {
  state.health = await api("/api/health");
  state.owned = await api("/api/owned");
  await loadPrinters();
  renderOwned();
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
      : [];
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
  await saveGroups([...state.groups, { name, files: [], manual_layout: null, arranged_3mf: null }]);
  $("group-name").value = "";
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
  await api("/api/poses", {
    method: "PUT",
    body: JSON.stringify({ path: state.selectedModel, rotation: null, position: null, z: null, scale: null }),
  });
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
bindGcodePointer();
$("reset-gcode-view").addEventListener("click", () => {
  state.gcodeFramed = false;
  drawLayer(Number($("layer-slider").value), true);
});

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
  api("/api/owned"),
  api("/api/filaments"),
]);
const curaReady = boot[0].cura_engine || boot[0].engine;
$("engine-status").textContent = `${curaReady ? "CuraEngine ready" : "CuraEngine was not found"} · ${boot[0].orca_engine ? "OrcaSlicer ready" : "OrcaSlicer was not found"}`;
state.health = boot[0];
state.options = boot[1];
state.settings = boot[2];
state.printers = boot[3];
state.owned = boot[9];
state.filaments = boot[10];
state.filamentEdit = boot[10].filaments[0]?.id || null;
state.models = boot[4];
state.locations = boot[5];
state.groups = boot[6].groups;
state.poses = boot[7];
renderSettings();
renderPrinters();
renderOwned();
renderPaths();
renderModels();
renderJob(boot[8]);
await showLayout();
if (boot[8].status === "running" || boot[8].status === "queued") pollJob();
