import { Component, input } from '@angular/core';
import { Horse } from './horse';
import { Icon } from './icon';

/** The agent's final message (Answer card). Until it comes the slot waits with the horse trotting: agent calls
 * are not streamed (D6), so the answer arrives whole. The footer, saying how the answer was made, is projected. The
 * answer is the model's plain text, so it is shown as text with its line breaks, never as HTML. */
@Component({
  selector: 'app-answer',
  imports: [Horse, Icon],
  template: `
    @if (text(); as text) {
      <div class="ag-section ag-answer">
        <div class="ag-answer-head"><app-horse />Answer</div>
        <p>{{ text }}</p>
        <div class="ag-answer-foot"><ng-content /></div>
      </div>
    } @else {
      <div class="ag-section">
        <div class="ag-answer-head"><app-horse state="working" />Answer</div>
        <div class="ag-answer-wait"><app-icon name="clock" />{{ wait() }}</div>
      </div>
    }
  `,
  styles: ':host { display: contents; } p { white-space: pre-line; }',
})
export class Answer {
  readonly text = input<string | null>(null);
  readonly wait = input('The answer appears when the last step finishes.');
}
