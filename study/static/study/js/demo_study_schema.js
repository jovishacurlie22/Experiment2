/* Demo schema for tutorial / screen-recording purposes only.
   NOT auto-generated, NOT used for real participants. Loaded on every
   page load ALONGSIDE the real study_schema.js / study_config.js, under
   its own globals (DEMO_STUDY_*) so it never overwrites them -- app.js
   picks between the two sets at runtime based on the logged-in
   participant ID (the "DEMO" account gets this set, everyone else gets
   the real STUDY_MODULES / STUDY_CONFIG_ASC / STUDY_CONFIG_DESC).

   Covers each question type/feature the real schema uses:
     numeric, nominal, text, multi (with an `exclusive` option),
     matrix, and two `showIf` skip-logic examples. */
window.DEMO_STUDY_MODULES = [
  {
    id: "demo-module-1",
    kind: "standard",
    title: "Demo Module A",
    sections: [
      {
        id: "demo-basics",
        title: "Basics",
        questions: [
          {
            id: "demo-q1",
            type: "numeric",
            stem: "How many hours of sleep did you get last night?",
            options: [],
          },
          {
            id: "demo-q2",
            type: "nominal",
            stem: "Which of these best describes your current mood?",
            options: [
              { value: "1", label: "Great" },
              { value: "2", label: "Okay" },
              { value: "3", label: "Tired" },
              { value: "4", label: "Stressed" },
            ],
          },
          {
            id: "demo-q3",
            type: "text",
            stem: "What's one word to describe today?",
            options: [],
          },
        ],
      },
      {
        id: "demo-preferences",
        title: "Preferences",
        questions: [
          {
            id: "demo-q4",
            type: "multi",
            stem: "Which of the following do you enjoy? (select all that apply)",
            options: [
              { value: "1", label: "Reading" },
              { value: "2", label: "Sports" },
              { value: "3", label: "Cooking" },
              { value: "4", label: "Gaming" },
              { value: "0", label: "None of the above", exclusive: true },
            ],
          },
          {
            // Skip logic example 1: only shown if q4 has at least one real
            // answer selected (i.e. "None of the above" was NOT chosen).
            id: "demo-q5",
            type: "nominal",
            stem: "How often do you make time for that activity?",
            options: [
              { value: "1", label: "Daily" },
              { value: "2", label: "A few times a week" },
              { value: "3", label: "Rarely" },
            ],
            showIf: { questionId: "demo-q4", excludesAny: ["0"] },
          },
        ],
      },
    ],
  },
  {
    id: "demo-module-2",
    kind: "standard",
    title: "Demo Module B",
    sections: [
      {
        id: "demo-matrix",
        title: "Agreement scale",
        questions: [
          {
            id: "demo-q6",
            type: "matrix",
            stem: "To what extent do you agree or disagree with each of the following statements:",
            items: [
              { id: "demo-q6-i0", label: "I find this demo easy to follow." },
              { id: "demo-q6-i1", label: "The layout is clear." },
            ],
            options: [
              { value: "1", label: "Strongly agree" },
              { value: "2", label: "Agree" },
              { value: "3", label: "Neither agree nor disagree" },
              { value: "4", label: "Disagree" },
              { value: "5", label: "Strongly disagree" },
            ],
          },
        ],
      },
      {
        id: "demo-wrapup",
        title: "Wrap-up",
        questions: [
          {
            // Skip logic example 2: only shown if q2's mood answer was
            // "Tired" or "Stressed".
            id: "demo-q7",
            type: "nominal",
            stem: "Would you like a short break before continuing?",
            options: [
              { value: "1", label: "Yes" },
              { value: "0", label: "No" },
            ],
            showIf: { questionId: "demo-q2", in: ["3", "4"] },
          },
          {
            id: "demo-q8",
            type: "text",
            stem: "Any final comments before we finish this demo?",
            options: [],
          },
        ],
      },
    ],
  },
];

window.DEMO_STUDY_CONFIG_ASC = {
  activeModuleIds: ["demo-module-1", "demo-module-2"],
};
window.DEMO_STUDY_CONFIG_DESC = {
  activeModuleIds: ["demo-module-2", "demo-module-1"],
};