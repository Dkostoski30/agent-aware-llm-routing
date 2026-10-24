# Confirmed topic

Approved by the mentor on 21.10.2026.

## Наслов

Рутирање на LLM повици и автоматско скалирање на vLLM инстанци во Kubernetes околина

## Опис

Оваа дипломска работа опфаќа развој на систем за рутирање на LLM повици и автоматско скалирање на vLLM инстанци во Kubernetes околина. Повиците од иста агентска задача се насочуваат кон инстанцата што го чува заедничкиот контекст во кеш, со што се намалува повторната обработка. При формирање редица, предност добиваат задачите поблиску до завршување, додека паралелните повици се пренасочуваат кон друга инстанца само при преоптоварување. Автоматското скалирање се заснова на проценката на преостанатата работа на активните задачи, овозможувајќи навремено активирање дополнителни инстанци. При намалување на бројот на инстанци, тековните задачи најпрво се завршуваат, а при откажување продолжуваат на друга инстанца преку зачуваната историја. Целта е да се намали времето за завршување на задачите и да се подобри искористеноста на ресурсите. Решението се споредува со стандардно рутирање и автоматско скалирање според искористеноста на CPU, преку мерење на времето на извршување, пропусната моќ и потрошувачката на ресурси.

## What the description promises, and where it's covered

| Promise in the description | Plan |
|---|---|
| Calls of one task go to the pod with the cached context | P3.3, D1 |
| Near-finished tasks get priority when there's a queue | P4.1, D3 |
| Parallel calls move to another pod only under overload | P3.5, P3.6 |
| Autoscaling on estimated remaining work | P3.8, P3.9, D2 |
| Scale-down finishes current tasks first | P4.3 |
| Failed pod: tasks continue on another pod from saved history | P4.4, D7 |
| Compared with standard routing and CPU-based autoscaling | P2.5, P3.10, P4.5, P5.1, P5.2 |
| Measured: execution time, throughput, resource usage | P5.1–P5.3 |
