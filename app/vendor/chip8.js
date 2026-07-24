/* GOST built-in CHIP-8 emulator -- written from scratch (build-your-own-x).
 * Runs the classic 64x32 VM entirely offline, rendered to a canvas in the
 * current theme's colours. ~35 opcodes, 60Hz timers, ~600Hz CPU. window.Chip8. */
(function () {
  // 4x5 hex font, loaded at 0x050
  const FONT = [
    0xF0,0x90,0x90,0x90,0xF0, 0x20,0x60,0x20,0x20,0x70, 0xF0,0x10,0xF0,0x80,0xF0,
    0xF0,0x10,0xF0,0x10,0xF0, 0x90,0x90,0xF0,0x10,0x10, 0xF0,0x80,0xF0,0x10,0xF0,
    0xF0,0x80,0xF0,0x90,0xF0, 0xF0,0x10,0x20,0x40,0x40, 0xF0,0x90,0xF0,0x90,0xF0,
    0xF0,0x90,0xF0,0x10,0xF0, 0xF0,0x90,0xF0,0x90,0x90, 0xE0,0x90,0xE0,0x90,0xE0,
    0xF0,0x80,0x80,0x80,0xF0, 0xE0,0x90,0x90,0x90,0xE0, 0xF0,0x80,0xF0,0x80,0xF0,
    0xF0,0x80,0xF0,0x80,0x80
  ];

  class Chip8 {
    constructor(canvas) {
      this.cv = canvas; this.ctx = canvas.getContext("2d");
      this.W = 64; this.H = 32;
      this.speed = 12;                 // CPU cycles per frame (~720Hz)
      this.running = false; this._raf = null;
      this.reset();
    }
    reset() {
      this.mem = new Uint8Array(4096);
      for (let i = 0; i < FONT.length; i++) this.mem[0x50 + i] = FONT[i];
      this.V = new Uint8Array(16);
      this.I = 0; this.pc = 0x200; this.stack = []; this.dt = 0; this.st = 0;
      this.gfx = new Uint8Array(this.W * this.H);
      this.keys = new Uint8Array(16); this.draw = true; this.waitKey = -1;
    }
    load(bytes) {
      this.reset();
      const n = Math.min(bytes.length, 4096 - 0x200);
      for (let i = 0; i < n; i++) this.mem[0x200 + i] = bytes[i] & 0xFF;
    }
    key(k, down) {
      if (k < 0 || k > 15) return;
      this.keys[k] = down ? 1 : 0;
      if (down && this.waitKey >= 0) { this.V[this.waitKey] = k; this.waitKey = -1; }
    }
    step() {
      if (this.waitKey >= 0) return;   // FX0A halts until a key is pressed
      const op = (this.mem[this.pc] << 8) | this.mem[this.pc + 1];
      this.pc = (this.pc + 2) & 0xFFF;
      const x = (op & 0x0F00) >> 8, y = (op & 0x00F0) >> 4;
      const n = op & 0xF, nn = op & 0xFF, nnn = op & 0xFFF, V = this.V;
      switch (op & 0xF000) {
        case 0x0000:
          if (op === 0x00E0) { this.gfx.fill(0); this.draw = true; }
          else if (op === 0x00EE) this.pc = (this.stack.pop() || 0x200) & 0xFFF;
          break;
        case 0x1000: this.pc = nnn; break;
        case 0x2000: this.stack.push(this.pc); this.pc = nnn; break;
        case 0x3000: if (V[x] === nn) this.pc = (this.pc + 2) & 0xFFF; break;
        case 0x4000: if (V[x] !== nn) this.pc = (this.pc + 2) & 0xFFF; break;
        case 0x5000: if (V[x] === V[y]) this.pc = (this.pc + 2) & 0xFFF; break;
        case 0x6000: V[x] = nn; break;
        case 0x7000: V[x] = (V[x] + nn) & 0xFF; break;
        case 0x8000: {
          const a = V[x], b = V[y];
          if (n === 0x0) V[x] = b;
          else if (n === 0x1) V[x] = a | b;
          else if (n === 0x2) V[x] = a & b;
          else if (n === 0x3) V[x] = a ^ b;
          else if (n === 0x4) { const s = a + b; V[x] = s & 0xFF; V[0xF] = s > 0xFF ? 1 : 0; }
          else if (n === 0x5) { V[x] = (a - b) & 0xFF; V[0xF] = a >= b ? 1 : 0; }
          else if (n === 0x6) { V[0xF] = a & 1; V[x] = a >> 1; }
          else if (n === 0x7) { V[x] = (b - a) & 0xFF; V[0xF] = b >= a ? 1 : 0; }
          else if (n === 0xE) { V[0xF] = (a >> 7) & 1; V[x] = (a << 1) & 0xFF; }
          break;
        }
        case 0x9000: if (V[x] !== V[y]) this.pc = (this.pc + 2) & 0xFFF; break;
        case 0xA000: this.I = nnn; break;
        case 0xB000: this.pc = (nnn + V[0]) & 0xFFF; break;
        case 0xC000: V[x] = (Math.random() * 256) & nn; break;
        case 0xD000: {                 // draw n-byte sprite at (Vx,Vy), XOR, wrap
          const px0 = V[x] % this.W, py0 = V[y] % this.H; V[0xF] = 0;
          for (let row = 0; row < n; row++) {
            const s = this.mem[(this.I + row) & 0xFFF];
            for (let col = 0; col < 8; col++) {
              if (s & (0x80 >> col)) {
                const idx = ((px0 + col) % this.W) + ((py0 + row) % this.H) * this.W;
                if (this.gfx[idx]) V[0xF] = 1;
                this.gfx[idx] ^= 1;
              }
            }
          }
          this.draw = true; break;
        }
        case 0xE000:
          if (nn === 0x9E) { if (this.keys[V[x] & 0xF]) this.pc = (this.pc + 2) & 0xFFF; }
          else if (nn === 0xA1) { if (!this.keys[V[x] & 0xF]) this.pc = (this.pc + 2) & 0xFFF; }
          break;
        case 0xF000:
          if (nn === 0x07) V[x] = this.dt;
          else if (nn === 0x0A) this.waitKey = x;
          else if (nn === 0x15) this.dt = V[x];
          else if (nn === 0x18) this.st = V[x];
          else if (nn === 0x1E) this.I = (this.I + V[x]) & 0xFFF;
          else if (nn === 0x29) this.I = 0x50 + (V[x] & 0xF) * 5;
          else if (nn === 0x33) { const v = V[x]; this.mem[this.I] = (v / 100) | 0; this.mem[this.I + 1] = ((v / 10) | 0) % 10; this.mem[this.I + 2] = v % 10; }
          else if (nn === 0x55) { for (let i = 0; i <= x; i++) this.mem[(this.I + i) & 0xFFF] = V[i]; }
          else if (nn === 0x65) { for (let i = 0; i <= x; i++) V[i] = this.mem[(this.I + i) & 0xFFF]; }
          break;
      }
    }
    render() {
      const cs = getComputedStyle(document.documentElement);
      const on = (cs.getPropertyValue("--pri").trim()) || "#ffb000";
      const off = (cs.getPropertyValue("--bg").trim()) || "#000";
      const ctx = this.ctx, cw = this.cv.width, ch = this.cv.height;
      const sx = cw / this.W, sy = ch / this.H;
      ctx.fillStyle = off; ctx.fillRect(0, 0, cw, ch);
      ctx.fillStyle = on;
      for (let i = 0; i < this.gfx.length; i++) {
        if (this.gfx[i]) ctx.fillRect((i % this.W) * sx, ((i / this.W) | 0) * sy, Math.ceil(sx), Math.ceil(sy));
      }
    }
    _frame() {
      if (!this.running) return;
      for (let i = 0; i < this.speed; i++) this.step();
      if (this.dt > 0) this.dt--;
      if (this.st > 0) this.st--;      // (sound timer counts down; no beep wired)
      if (this.draw) { this.render(); this.draw = false; }
      this._raf = requestAnimationFrame(() => this._frame());
    }
    start() { if (this.running) return; this.running = true; this._raf = requestAnimationFrame(() => this._frame()); }
    stop() { this.running = false; if (this._raf) cancelAnimationFrame(this._raf); this._raf = null; }
  }
  window.Chip8 = Chip8;
})();
