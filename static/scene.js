/* ==========================================================================
   The living background.
   --------------------------------------------------------------------------
   static/img/scene.png is a 640x357 pixel-grid painting. It is blown up at an
   integer scale (see .scene-art in style.css) and this canvas is drawn on that
   very same grid, so every cloud, bird, waterfall streak and glint lands on
   whole art pixels — the way a sprite would on real 8-bit hardware. Motion is
   stored in painting pixels or normalised coordinates so the ambience stays
   glued to the mountains and towers no matter how the window is shaped, and
   everything is rounded to whole pixels before it is drawn.

   The waterfall streaks run along water columns measured pixel-by-pixel out
   of the painting itself (FALL_SEGMENTS below), so the water flows where the
   artist painted it and nowhere else.

   Nothing here loads anything or touches the app's own logic; it just makes
   the picture breathe. Under prefers-reduced-motion it draws one still frame.
   ========================================================================== */

const canvas = document.querySelector(".scene-canvas");
const art = document.querySelector(".scene-art");

const ART_WIDTH = 640;
const ART_HEIGHT = 357;

const CLOUD_LIGHT = "#fff8ef";
const CLOUD_MID = "#f0dfd2";
const CLOUD_SHADE = "#d5c6dd";
const BIRD_INK = "#3b2f52";
const WATER_WHITE = "#f4f9ff";
const WATER_SHADE = "#c2dcf0";
const MIST = "#eef6ff";
const PETAL_COLORS = ["#f2b8cf", "#f6e07a", "#f4ead2", "#c8b6e8"];
const BUTTERFLY_COLORS = ["#f2b8cf", "#f6e07a", "#eef3ff", "#a8c8f0"];

/* Sprites are char maps: '.' is transparent, letters index into a palette. */
const CLOUD_SPRITES = [
  {
    palette: { W: CLOUD_LIGHT, S: CLOUD_SHADE, M: CLOUD_MID },
    rows: [
      "....WWWW....",
      "..WWWWWWWW..",
      ".WWWWWWWWWW.",
      "WWWWWWWWWWWW",
      "WMMWWWWWWMMW",
      ".SSMMMMMMSS.",
    ],
  },
  {
    palette: { W: CLOUD_LIGHT, S: CLOUD_SHADE, M: CLOUD_MID },
    rows: [
      "...WWW...",
      ".WWWWWWW.",
      "WWWWWWWWW",
      "WMMWWWWWMM",
      ".SSMMMMS..",
    ],
  },
  {
    palette: { W: CLOUD_LIGHT, S: CLOUD_SHADE, M: CLOUD_MID },
    rows: [
      "......WW......",
      "...WWWWWWWW...",
      ".WWWWWWWWWWWW.",
      "WWWWWWWWWWWWWW",
      "MWWWWWWWWWWWW.",
      ".SSMMMMMMMMS..",
    ],
  },
];

const BIRD_FRAMES = [
  [
    "#...#",
    ".#.#.",
    "..#..",
  ],
  [
    ".....",
    ".#.#.",
    "#####",
  ],
];

const BUTTERFLY_FRAMES = [
  [".W.W", "WWWW", ".W.W"],
  ["..W.", ".WW.", "..W."],
];

/* The sailboat: tiny on purpose, so the castle stays monumental. It faces
   left, the way it travels. */
const BOAT_SPRITE = {
  palette: {
    W: "#f6ead2",
    S: "#d9c2a0",
    M: "#3b2f52",
    H: "#6b4a33",
    B: "#432f26",
  },
  rows: [
    "....M..",
    "...WM..",
    "..WWM..",
    ".WWSM..",
    "HHHHHHH",
    ".BBBBB.",
  ],
};

/* The crossing, measured against the open water of the painting: it starts
   behind the castle cliffs at the right edge and ends off the left edge of
   the screen, where the lake runs out of frame. The bow lifts the middle of
   the route a few pixels over the islands. */
const BOAT_PATH = {
  x0: 646,
  y0: 333,
  x1: -30,
  y1: 262,
  bow: 7,
};
const BOAT_PATH_LENGTH = Math.hypot(
  BOAT_PATH.x1 - BOAT_PATH.x0,
  BOAT_PATH.y1 - BOAT_PATH.y0,
);

function boatPosition(t) {
  return [
    BOAT_PATH.x0 + (BOAT_PATH.x1 - BOAT_PATH.x0) * t,
    BOAT_PATH.y0 +
      (BOAT_PATH.y1 - BOAT_PATH.y0) * t -
      Math.sin(Math.PI * t) * BOAT_PATH.bow,
  ];
}

/* Waterfall water, measured column by column out of the painting: each entry
   is one art pixel column x with the vertical run of water y0..y1 that the
   artist painted there. Streaks travel along these runs and can never drift
   off the falls. */
const FALL_SEGMENTS =
  "470:253-273;471:252-256;473:248-250;474:243-249;476:231-242;" +
  "477:222-224;477:229-232;478:219-228;495:263-265;496:211-234;" +
  "496:239-262;497:211-219;497:240-244;502:220-238;503:216-222;" +
  "504:210-215;549:251-253;556:284-287;557:282-287;559:281-286;" +
  "560:282-286;561:257-263;561:279-286;562:252-261;562:281-284;" +
  "563:251-260;563:283-285;564:250-261;564:281-287;565:250-263;" +
  "566:249-255;566:285-287;567:249-251;573:280-282;574:280-284;" +
  "575:283-287;576:279-283";

const SEGMENTS = FALL_SEGMENTS.split(";").map((entry) => {
  const [x, run] = entry.split(":");
  const [y0, y1] = run.split("-");
  return { x: Number(x), y0: Number(y0), y1: Number(y1) };
});
/* Where each cascade lands: the lowest runs get the mist. */
const FALL_BASES = SEGMENTS.filter((segment) => segment.y1 >= 265);

/* Stained glass, in art pixels, measured pane by pane out of the painting:
   every pane colour that exists — red, orange, gold, green, teal, blue —
   gets its own glint points, so any pane can catch the light. */
const GLASS_PANES = [
  /* the tall west window */
  { x: 420, y: 151, color: "#ff8d7d" },
  { x: 419, y: 160, color: "#ff8d7d" },
  { x: 419, y: 171, color: "#ff8d7d" },
  { x: 418, y: 162, color: "#ffa25e" },
  { x: 429, y: 145, color: "#ffa25e" },
  { x: 422, y: 152, color: "#ffd271" },
  { x: 421, y: 168, color: "#7fe0a0" },
  { x: 418, y: 167, color: "#7fd6e8" },
  /* the gate window */
  { x: 487, y: 170, color: "#ff8d7d" },
  { x: 487, y: 167, color: "#ffa25e" },
  { x: 483, y: 172, color: "#ffd271" },
  { x: 481, y: 176, color: "#7fe0a0" },
  /* the chapel window */
  { x: 527, y: 186, color: "#ff8d7d" },
  { x: 523, y: 179, color: "#ffa25e" },
  { x: 532, y: 183, color: "#ffd271" },
  /* the tower window */
  { x: 558, y: 197, color: "#ff8d7d" },
  { x: 553, y: 193, color: "#ff8d7d" },
  { x: 555, y: 193, color: "#ffa25e" },
  { x: 556, y: 205, color: "#ffd271" },
  { x: 550, y: 187, color: "#7fd6e8" },
  { x: 562, y: 208, color: "#8fb8ff" },
];

/* The meadow, home of the butterflies. */
const MEADOW = { x0: 2, x1: 218, y0: 285, y1: 352 };
const SKY = { x0: -64, x1: 704, y0: 7, y1: 121 };

/* Clouds are drawn a few pixels per sprite cell so they read at the same
   chunky scale as the painted clouds behind them. */
const CLOUD_ZOOM = 3;

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

let context = canvas.getContext("2d");
let scale = 1;
let sceneLeft = 0;
let sceneTop = 0;
let parallax = 0;
let running = false;
let lastTime = 0;
let lastDraw = 0;
let glintTimer = 0;

const clouds = [];
const birds = [];
const streaks = [];
const mists = [];
const glints = [];
const petals = [];
const motes = [];
const butterflies = [];
const wake = [];

const boat = { t: 0, speed: 6, bob: 0, idle: 0 };
let wakeTimer = 0;

/* --- helpers ----------------------------------------------------------- */

function randomBetween(low, high) {
  return low + Math.random() * (high - low);
}

function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

function flipCoin() {
  return Math.random() < 0.5;
}

/* Painting pixels -> whole pixels on the canvas grid. The painting drifts
   with the page scroll (see the scroll handler), so the drift is folded in
   here and the sprites stay glued to their mountains. */
function toCanvasPx(px, py) {
  return [
    Math.round(sceneLeft + px),
    Math.round(sceneTop + py + parallax),
  ];
}

function toCanvas(nx, ny) {
  return toCanvasPx(nx * ART_WIDTH, ny * ART_HEIGHT);
}

function paintSprite(rows, palette, x, y, flip, zoom = 1) {
  const width = rows[0].length;
  rows.forEach((row, rowIndex) => {
    for (let column = 0; column < width; column += 1) {
      const key = row[flip ? width - 1 - column : column];
      const color = palette[key];
      if (!color) continue;
      context.fillStyle = color;
      context.fillRect(
        x + column * zoom,
        y + rowIndex * zoom,
        zoom,
        zoom,
      );
    }
  });
}

/* --- spawning ---------------------------------------------------------- */

function makeCloud() {
  return {
    nx: randomBetween(-0.1, 1.1),
    ny: randomBetween(SKY.y0 / ART_HEIGHT, SKY.y1 / ART_HEIGHT),
    speed: (flipCoin() ? 1 : -1) * randomBetween(0.0022, 0.0055),
    sprite: pick(CLOUD_SPRITES),
    flip: flipCoin(),
  };
}

function spawnFlock() {
  const fromLeft = flipCoin();
  const size = 1 + Math.floor(Math.random() * 3);
  for (let index = 0; index < size; index += 1) {
    birds.push({
      nx: (fromLeft ? -0.08 : 1.08) - index * 0.035,
      ny: randomBetween(0.03, 0.28) + index * 0.012,
      speed: (fromLeft ? 1 : -1) * randomBetween(0.028, 0.05),
      phase: index * 0.35,
    });
  }
}

function makeButterfly() {
  return {
    px: randomBetween(MEADOW.x0, MEADOW.x1),
    py: randomBetween(MEADOW.y0, MEADOW.y1),
    phase: Math.random() * Math.PI * 2,
    speed: randomBetween(0.55, 0.9),
    color: pick(BUTTERFLY_COLORS),
  };
}

function populate() {
  clouds.length = 0;
  for (let index = 0; index < 5; index += 1) {
    clouds.push(makeCloud());
  }

  birds.length = 0;

  streaks.length = 0;
  SEGMENTS.forEach((segment) => {
    const count = segment.y1 - segment.y0 > 12 ? 2 : 1;
    for (let index = 0; index < count; index += 1) {
      streaks.push({
        segment,
        y: randomBetween(segment.y0, segment.y1),
        speed: randomBetween(16, 30),
        length: Math.min(segment.y1 - segment.y0, randomBetween(2, 5)),
      });
    }
  });

  mists.length = 0;
  FALL_BASES.forEach((base) => {
    for (let index = 0; index < 2; index += 1) {
      mists.push({
        px: base.x + randomBetween(-2, 2),
        py: base.y1 + randomBetween(0, 2),
        life: Math.random(),
        speed: randomBetween(0.35, 0.6),
        drift: randomBetween(-1.4, 1.4),
      });
    }
  });

  petals.length = 0;
  for (let index = 0; index < 14; index += 1) {
    petals.push({
      px: randomBetween(-30, MEADOW.x1),
      py: randomBetween(MEADOW.y0, MEADOW.y1 + 8),
      speed: randomBetween(4, 9),
      sway: randomBetween(2, 5),
      phase: Math.random() * Math.PI * 2,
      color: pick(PETAL_COLORS),
    });
  }

  butterflies.length = 0;
  for (let index = 0; index < 4; index += 1) {
    butterflies.push(makeButterfly());
  }

  motes.length = 0;
  for (let index = 0; index < 10; index += 1) {
    motes.push({
      nx: Math.random(),
      ny: Math.random(),
      speed: randomBetween(0.004, 0.01),
      phase: Math.random() * Math.PI * 2,
    });
  }

  boat.t = randomBetween(0, 0.3);
  boat.speed = randomBetween(5.5, 7.5);
  boat.bob = 0;
  boat.idle = 0;
  wake.length = 0;
  wakeTimer = randomBetween(0.2, 1);
}

/* --- updating ---------------------------------------------------------- */

function update(delta) {
  clouds.forEach((cloud) => {
    cloud.nx += cloud.speed * delta;
    if (cloud.speed > 0 && cloud.nx > 1.18) cloud.nx = -0.18;
    if (cloud.speed < 0 && cloud.nx < -0.18) cloud.nx = 1.18;
  });

  if (birds.length === 0 && Math.random() < delta / 16) spawnFlock();
  birds.forEach((bird) => {
    bird.nx += bird.speed * delta;
    bird.phase += delta;
  });
  for (let index = birds.length - 1; index >= 0; index -= 1) {
    const bird = birds[index];
    if (bird.speed > 0 ? bird.nx > 1.15 : bird.nx < -0.15) birds.splice(index, 1);
  }

  /* Water: each streak slides down its own painted column and respawns at
     the top of that same column. */
  streaks.forEach((streak) => {
    streak.y += streak.speed * delta;
    if (streak.y > streak.segment.y1) {
      streak.y = streak.segment.y0 - streak.length;
      streak.speed = randomBetween(16, 30);
    }
  });

  mists.forEach((mist) => {
    mist.life += mist.speed * delta;
    if (mist.life > 1) {
      const base = pick(FALL_BASES);
      mist.life = 0;
      mist.px = base.x + randomBetween(-2, 2);
      mist.py = base.y1 + randomBetween(0, 2);
    }
    mist.px += mist.drift * delta;
  });

  /* Glints: a random pane catches the light for a brief moment. Never more
     than two at a time, and never on a schedule you can learn. */
  glintTimer -= delta;
  if (glintTimer <= 0) {
    if (glints.length < 2) {
      const pane = pick(GLASS_PANES);
      glints.push({
        px: pane.x + randomBetween(-1.5, 1.5),
        py: pane.y + randomBetween(-2, 2),
        color: pane.color,
        life: 0,
        speed: randomBetween(2, 2.8),
      });
    }
    glintTimer = randomBetween(1.1, 3.2);
  }
  for (let index = glints.length - 1; index >= 0; index -= 1) {
    const glint = glints[index];
    glint.life += glint.speed * delta;
    if (glint.life > 1) glints.splice(index, 1);
  }

  petals.forEach((petal) => {
    petal.phase += delta * 1.6;
    petal.px += petal.sway * Math.sin(petal.phase) * delta + delta * 1.5;
    petal.py += petal.speed * delta;
    if (petal.py > MEADOW.y1 + 8) {
      petal.py = MEADOW.y0 - 8;
      petal.px = randomBetween(-30, MEADOW.x1);
    }
    if (petal.px > MEADOW.x1) petal.px = -30;
  });

  butterflies.forEach((butterfly) => {
    butterfly.phase += delta * butterfly.speed;
    butterfly.px += Math.sin(butterfly.phase) * delta * 6;
    butterfly.py += Math.cos(butterfly.phase * 0.7) * delta * 3.5;
    if (butterfly.px < MEADOW.x0) butterfly.px = MEADOW.x1;
    if (butterfly.px > MEADOW.x1) butterfly.px = MEADOW.x0;
    if (butterfly.py < MEADOW.y0) butterfly.py = MEADOW.y1;
    if (butterfly.py > MEADOW.y1) butterfly.py = MEADOW.y0;
  });

  /* The sailboat: one slow crossing behind the castle, then a long pause
     before it comes round again. */
  if (boat.idle > 0) {
    boat.idle -= delta;
    if (boat.idle <= 0) {
      boat.t = 0;
      boat.speed = randomBetween(5.5, 7.5);
    }
  } else {
    boat.t += (boat.speed / BOAT_PATH_LENGTH) * delta;
    boat.bob += delta;
    wakeTimer -= delta;
    if (wakeTimer <= 0) {
      // rings form at the stern, where the hull actually disturbs the water
      const [px, py] = boatPosition(Math.min(boat.t, 1));
      wake.push({ px: px + 3, py, life: 0, speed: randomBetween(0.18, 0.24) });
      wakeTimer = randomBetween(1.2, 1.9);
    }
    if (boat.t >= 1) boat.idle = randomBetween(18, 38);
  }

  /* The wake: rings left where the hull passed, widening slowly and fading
     out — small waves for a small boat. */
  for (let index = wake.length - 1; index >= 0; index -= 1) {
    const ring = wake[index];
    ring.life += ring.speed * delta;
    ring.px += delta * 0.8;
    if (ring.life > 1) wake.splice(index, 1);
  }

  motes.forEach((mote) => {
    mote.phase += delta * 0.7;
    mote.ny -= mote.speed * delta * 0.25;
    mote.nx += Math.sin(mote.phase) * delta * 0.004;
    if (mote.ny < -0.05) {
      mote.ny = 1.05;
      mote.nx = Math.random();
    }
  });
}

/* --- drawing ----------------------------------------------------------- */

function drawClouds() {
  clouds.forEach((cloud) => {
    const width = cloud.sprite.rows[0].length * CLOUD_ZOOM;
    const [x, y] = toCanvas(cloud.nx, cloud.ny);
    paintSprite(
      cloud.sprite.rows,
      cloud.sprite.palette,
      x - Math.round(width / 2),
      y,
      cloud.flip,
      CLOUD_ZOOM,
    );
  });
}

function drawBirds() {
  const frame = BIRD_FRAMES[Math.floor(performance.now() / 260) % BIRD_FRAMES.length];
  birds.forEach((bird) => {
    const [x, y] = toCanvas(bird.nx, bird.ny);
    paintSprite(frame, { "#": BIRD_INK }, x - 2, y, bird.speed < 0);
  });
}

function drawStreaks() {
  streaks.forEach((streak) => {
    const [x, y] = toCanvasPx(streak.segment.x, streak.y);
    const length = Math.max(2, Math.round(streak.length));
    context.globalAlpha = 0.85;
    context.fillStyle = WATER_WHITE;
    context.fillRect(x, y, 1, length);
    context.globalAlpha = 0.5;
    context.fillStyle = WATER_SHADE;
    context.fillRect(x + 1, y + 1, 1, Math.max(1, length - 1));
    context.globalAlpha = 1;
  });
}

function drawMist() {
  mists.forEach((mist) => {
    if (mist.life > 0.7) return;
    const [x, y] = toCanvasPx(mist.px, mist.py);
    const radius = 1 + Math.round(mist.life * 2);
    context.globalAlpha = 0.35 * (1 - mist.life / 0.7);
    context.fillStyle = MIST;
    context.fillRect(x - radius, y - Math.round(radius / 2), radius * 2, radius);
    context.globalAlpha = 1;
  });
}

function drawGlints() {
  glints.forEach((glint) => {
    const [x, y] = toCanvasPx(glint.px, glint.py);
    context.globalAlpha =
      glint.life < 0.2 ? 1 : Math.max(0, 1 - (glint.life - 0.2) / 0.8);
    context.fillStyle = glint.color;
    if (glint.life > 0.18 && glint.life < 0.62) {
      /* the bright middle of the flash: a tiny pixel cross */
      context.fillRect(x, y - 2, 1, 5);
      context.fillRect(x - 2, y, 5, 1);
    } else {
      context.fillRect(x, y, 1, 1);
    }
    context.globalAlpha = 1;
  });
}

function drawPetals() {
  petals.forEach((petal) => {
    const [x, y] = toCanvasPx(petal.px, petal.py);
    context.fillStyle = petal.color;
    context.fillRect(x, y, 1, 1);
  });
}

function drawButterflies() {
  const frame =
    BUTTERFLY_FRAMES[Math.floor(performance.now() / 240) % BUTTERFLY_FRAMES.length];
  butterflies.forEach((butterfly) => {
    const [x, y] = toCanvasPx(butterfly.px, butterfly.py);
    paintSprite(frame, { W: butterfly.color }, x - 2, y - 1);
  });
}

function drawWake() {
  /* Small waves cast off the stern: each ring widens as it travels back,
     its crest thickening once and then dissolving into the water. */
  wake.forEach((ring) => {
    const [cx, cy] = toCanvasPx(ring.px, ring.py + 2);
    const rx = 2 + Math.round(ring.life * 10);
    const ry = Math.max(1, Math.round(rx * 0.42));
    context.globalAlpha = 0.6 * (1 - ring.life) * (1 - ring.life * 0.4);
    context.fillStyle = WATER_WHITE;
    const dots = 18;
    for (let step = 0; step < dots; step += 1) {
      const angle = (step / dots) * Math.PI * 2;
      const x = cx + Math.round(rx * Math.cos(angle));
      const y = cy + Math.round(ry * Math.sin(angle));
      context.fillRect(x, y, 1, 1);
      if (ring.life > 0.35 && ring.life < 0.75) context.fillRect(x, y + 1, 1, 1);
    }
    context.globalAlpha = 1;
  });
}

function drawBoat() {
  if (boat.idle > 0) return;
  const [bx, by] = boatPosition(Math.min(boat.t, 1));
  const bob = Math.sin(boat.bob * 1.3) > 0 ? 0 : 1;
  const [x, y] = toCanvasPx(bx, by + bob);
  paintSprite(BOAT_SPRITE.rows, BOAT_SPRITE.palette, x - 3, y - 5);
}

function drawMotes() {
  motes.forEach((mote) => {
    const [x, y] = toCanvas(mote.nx, mote.ny);
    context.globalAlpha = 0.4;
    context.fillStyle = "#fff6df";
    context.fillRect(x, y, 1, 1);
    context.globalAlpha = 1;
  });
}

function draw() {
  context.clearRect(0, 0, canvas.width, canvas.height);
  drawClouds();
  drawBirds();
  drawStreaks();
  drawMist();
  drawGlints();
  drawWake();
  drawBoat();
  drawPetals();
  drawButterflies();
  drawMotes();
}

/* --- layout & loop ----------------------------------------------------- */

function layout() {
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;

  /* The painting fills the window edge to edge at its own proportions: the
     scale that covers the viewport exactly, so as much of the picture as can
     fit is on screen. It is no longer forced to a whole number -- art pixels
     vary by a fraction of a pixel on some window sizes, which is the price of
     not zooming in past the painting's edges. */
  scale = Math.max(viewportWidth / ART_WIDTH, viewportHeight / ART_HEIGHT, 1);

  const displayWidth = ART_WIDTH * scale;
  const displayHeight = ART_HEIGHT * scale;
  art.style.width = `${displayWidth}px`;
  art.style.height = `${displayHeight}px`;

  canvas.width = Math.ceil(viewportWidth / scale);
  canvas.height = Math.ceil(viewportHeight / scale);
  /* The canvas shares the art's grid exactly: one canvas pixel per art pixel,
     scaled by the same factor and centered the same way. */
  canvas.style.width = `${canvas.width * scale}px`;
  canvas.style.height = `${canvas.height * scale}px`;
  context = canvas.getContext("2d");
  context.imageSmoothingEnabled = false;

  sceneLeft = (viewportWidth / scale - ART_WIDTH) / 2;
  sceneTop = (viewportHeight / scale - ART_HEIGHT) / 2;
}

function frame(time) {
  if (!running) return;
  window.requestAnimationFrame(frame);
  /* Sprite motion lands on whole pixels anyway, so 30fps is visually
     identical here and costs half the CPU/GPU of 60. */
  if (time - lastDraw < 1000 / 30 - 1) return;
  lastDraw = time;
  const delta = Math.min(0.1, lastTime ? (time - lastTime) / 1000 : 0.016);
  lastTime = time;
  update(delta);
  draw();
}

function start() {
  if (running || reduceMotion.matches) return;
  running = true;
  lastTime = 0;
  window.requestAnimationFrame(frame);
}

function stop() {
  running = false;
}

function stillFrame() {
  /* Reduced motion: one quiet frame of the world, no animation loop. */
  draw();
}

/* --- wiring ------------------------------------------------------------ */

layout();
populate();

if (reduceMotion.matches) {
  stillFrame();
} else {
  start();
}

reduceMotion.addEventListener("change", () => {
  if (reduceMotion.matches) {
    stop();
    stillFrame();
  } else {
    start();
  }
});

window.addEventListener("resize", () => {
  layout();
  if (reduceMotion.matches) stillFrame();
});

window.addEventListener(
  "scroll",
  () => {
    /* The painting drifts a little slower than the page, like a parallax
       background on an overworld map. Reduced motion keeps it nailed down. */
    const shift = reduceMotion.matches ? 0 : Math.round(Math.min(48, window.scrollY * 0.06));
    if (shift / scale === parallax) return;
    parallax = shift / scale;
    art.style.setProperty("--parallax", `${shift}px`);
  },
  { passive: true },
);
