import assert from "node:assert/strict";
import test from "node:test";
import { makeQuiz } from "../src/quiz.js";

const flowLesson = {
  scenes: [{
    type: "diagram",
    diagram_kind: "flow",
    heading: "Water cycle",
    items: ["Evaporation", "Clouds", "Rain", "Evaporation"],
  }],
};

test("quiz questions use distinct diagram answers and valid choices", () => {
  const quiz = makeQuiz(flowLesson, 0);
  assert.equal(quiz.length, 3);
  for (const question of quiz) {
    assert.ok(question.options.includes(question.answer));
    assert.equal(new Set(question.options).size, question.options.length);
  }
});

test("starting a new quiz advances to a different question set when available", () => {
  const first = makeQuiz(flowLesson, 0).map((question) => question.prompt);
  const next = makeQuiz(flowLesson, 1).map((question) => question.prompt);
  assert.notDeepEqual(next, first);
});

test("quiz handles missing diagrams without crashing", () => {
  assert.deepEqual(makeQuiz({ scenes: [{ type: "title_card", items: [] }] }, 0), []);
});
