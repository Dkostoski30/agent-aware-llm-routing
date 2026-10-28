import { Scenario } from '../ui/scenario-card';

// The three demo scenarios (README: NewTaskScreen), in this order, with the preview's examples. Tool names are
// spelled as in the agent code; P2.1 adds the contracts and support tools.
export const SCENARIOS: Scenario[] = [
  {
    icon: 'table',
    title: 'CSV analysis',
    prompt: 'Which region grew revenue fastest from Q2 to Q3, and by how much?',
    source: 'sales_2025.csv',
    tools: ['csv_summary', 'group_by', 'compare'],
  },
  {
    icon: 'doc',
    title: 'Contracts',
    prompt:
      'Compare the termination clauses in contract A and contract B. Which one is riskier for us?',
    source: 'contract_a.md · contract_b.md',
    tools: ['read_document', 'extract_clauses', 'diff_clauses'],
  },
  {
    icon: 'chat',
    title: 'Support',
    prompt: 'Order 10482 arrived damaged. Draft a reply that follows our returns policy.',
    source: 'orders.json · policies.md',
    tools: ['get_order', 'get_policy', 'draft_reply'],
  },
];
