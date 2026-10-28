import { Component, input } from '@angular/core';

// The pixel horse from bundle.js: 14 x 12 art pixels, '#' filled, 'o' the eye (a hole). Rows 0-6 never move;
// rows 7-11 are the leg frames. bundle.css animates the frames and the blink.
const TOP = [
  '..........##..',
  '.........####.',
  '........##o###',
  '........######',
  '.......####..#',
  '......####....',
  '#########.....',
];
const STAND = [
  '#.#######.....',
  '#.#######.....',
  '..#.#..#.#....',
  '..#.#..#.#....',
  '..#.#..#.#....',
];
const TROT_A = [
  '#.#######.....',
  '#.#######.....',
  '..#.#..#.#....',
  '...##..#..#...',
  '....#..#......',
];
const TROT_B = [
  '#.#######.....',
  '..#######.....',
  '..#.#..#.#....',
  '..#..#..##....',
  '..#......#....',
];
const LEGS_FIRST_ROW = TOP.length;

/** One SVG path with a 1 x n rectangle per run of '#' in each row. */
function pixels(rows: string[], y0: number): string {
  let d = '';
  rows.forEach((row, i) => {
    let x = 0;
    while (x < row.length) {
      if (row[x] === '#') {
        const start = x;
        while (row[x] === '#') x++;
        d += `M${start} ${y0 + i}h${x - start}v1h-${x - start}z`;
      } else x++;
    }
  });
  return d;
}

const PATHS = {
  top: pixels(TOP, 0),
  stand: pixels(STAND, LEGS_FIRST_ROW),
  trotA: pixels(TROT_A, LEGS_FIRST_ROW),
  trotB: pixels(TROT_B, LEGS_FIRST_ROW),
};

/** The avatar (Avatar card): idle blinks, working trots. Sizes are whole multiples of 14px; no size is 2x. */
@Component({
  selector: 'app-horse',
  template: `
    <svg
      class="ag-horse"
      [class]="size() ? 'ag-horse--' + size() : ''"
      viewBox="0 0 14 12"
      [attr.data-state]="state()"
      role="img"
      [attr.aria-label]="label()"
    >
      <path [attr.d]="paths.top" />
      <path class="stand" [attr.d]="paths.stand" />
      <path class="trot-a" [attr.d]="paths.trotA" />
      <path class="trot-b" [attr.d]="paths.trotB" />
      <rect class="lid" x="10" y="2" width="1" height="1" />
    </svg>
  `,
  styles: ':host { display: contents; }',
})
export class Horse {
  readonly state = input<'idle' | 'working'>('idle');
  readonly size = input<'1x' | '4x' | '8x'>();
  readonly label = input('Tenax');
  protected readonly paths = PATHS;
}
