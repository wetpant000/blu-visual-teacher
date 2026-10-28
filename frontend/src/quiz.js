const shuffled = (items) => {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
};

export function makeQuiz(lesson, round = 0) {
  const diagrams = lesson?.scenes.filter((scene) => scene.type === "diagram") || [];
  const questions = [];
  for (const diagram of diagrams) {
    const items = [...new Set(diagram.items.filter(Boolean))];
    if (items.length < 2) continue;
    const { diagram_kind: kind, heading } = diagram;
    const addPositionQuestion = (prompt, index, explanation) => questions.push({
      prompt,
      options: shuffled(items),
      answer: items[index],
      explanation,
    });

    if (kind === "network") {
      addPositionQuestion(`In "${heading}", which idea is at the center?`, 0,
        `${items[0]} is the central idea; the other ideas connect to it.`);
      items.slice(1).forEach((item) => {
        const correct = `${item} connects to ${items[0]}`;
        questions.push({
          prompt: `How does "${item}" relate to the center of "${heading}"?`,
          options: shuffled([correct, `${item} is unrelated to ${items[0]}`, `${item} happens after ${items[0]}`]),
          answer: correct,
          explanation: `${item} is shown as a connected idea around ${items[0]}.`,
        });
      });
    } else if (kind === "cycle") {
      items.forEach((item, index) => {
        const next = items[(index + 1) % items.length];
        const previous = items[(index + items.length - 1) % items.length];
        addPositionQuestion(`In "${heading}", which stage follows "${item}"?`, (index + 1) % items.length,
          `${next} follows ${item} in the cycle.`);
        addPositionQuestion(`In "${heading}", which stage comes before "${item}"?`, (index + items.length - 1) % items.length,
          `${previous} comes before ${item} in the cycle.`);
      });
    } else if (kind === "layers") {
      items.forEach((item, index) => {
        addPositionQuestion(`In "${heading}", what is at layer ${index + 1} of ${items.length}?`, index,
          `${item} is shown at layer ${index + 1} of ${items.length}, from top to bottom.`);
      });
    } else {
      addPositionQuestion(`In "${heading}", which idea comes first?`, 0,
        `${items[0]} is the first step in the diagram.`);
      addPositionQuestion(`In "${heading}", which idea comes last?`, items.length - 1,
        `${items[items.length - 1]} is the final step in the diagram.`);
      items.slice(0, -1).forEach((item, index) => {
        addPositionQuestion(`In "${heading}", which idea follows "${item}"?`, index + 1,
          `${items[index + 1]} follows ${item} in the diagram.`);
      });
      items.slice(1).forEach((item, index) => {
        addPositionQuestion(`In "${heading}", which idea comes before "${item}"?`, index,
          `${items[index]} comes before ${item} in the diagram.`);
      });
    }
  }

  if (!questions.length) return [];
  const count = Math.min(3, questions.length);
  const start = (round * count) % questions.length;
  return Array.from({ length: count }, (_, index) => questions[(start + index) % questions.length]);
}
