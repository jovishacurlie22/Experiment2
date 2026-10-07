/* Demo schema for tutorial / screen-recording purposes only.
   NOT auto-generated, NOT used for real participants, and intentionally
   generic — the wording here is unrelated to the real HMS/MECAMH/
   Demographics content participants actually see, so nothing shown in a
   recording overlaps with or resembles the real instrument.

   Loaded on every page load ALONGSIDE the real study_schema.js /
   study_config.js, under its own globals (DEMO_STUDY_*) so it never
   overwrites them -- app.js picks between the two sets at runtime based
   on the logged-in participant ID (the "DEMO" account gets this set,
   everyone else gets the real STUDY_MODULES / STUDY_CONFIG_ASC /
   STUDY_CONFIG_DESC).

   Covers each question type the real engine supports, except open text:
     numeric, dropdown, nominal, binary, ordinal, multi (with an
     `exclusive` option), matrix, and two `showIf` skip-logic examples. */
window.DEMO_STUDY_MODULES = [
  {
    id: "demo-module-1",
    kind: "standard",
    title: "Healthcare Visits",
    sections: [
      {
        id: "demo-visit-history",
        title: "Visit History",
        questions: [
          {
            id: "demo-q1",
            type: "numeric",
            stem: "In the past year, how many times have you visited a doctor or clinic?",
            options: [],
          },
          {
            id: "demo-q2",
            type: "dropdown",
            stem: "When was the last time you visited a doctor?",
            options: [
              { value: "1", label: "Within the last month" },
              { value: "2", label: "1 – 3 months ago" },
              { value: "3", label: "3 – 6 months ago" },
              { value: "4", label: "6 – 12 months ago" },
              { value: "5", label: "More than a year ago" },
              { value: "6", label: "Never" },
            ],
          },
          {
            id: "demo-q3",
            type: "nominal",
            stem: "What was the main reason for your most recent visit?",
            options: [
              { value: "1", label: "Routine check-up" },
              { value: "2", label: "Illness or infection" },
              { value: "3", label: "Injury" },
              { value: "4", label: "Follow-up appointment" },
              { value: "5", label: "Other" },
            ],
          },
          {
            id: "demo-q4",
            type: "binary",
            stem: "Do you currently have a primary care doctor?",
            options: [
              { value: "1", label: "Yes" },
              { value: "0", label: "No" },
            ],
          },
        ],
      },
      {
        id: "demo-preventive-care",
        title: "Preventive Care",
        questions: [
          {
            id: "demo-q5",
            type: "multi",
            stem: "Which of these have you had in the past year? (select all that apply)",
            options: [
              { value: "1", label: "Annual check-up" },
              { value: "2", label: "Dental check-up" },
              { value: "3", label: "Eye exam" },
              { value: "4", label: "Vaccination or flu shot" },
              { value: "5", label: "Blood test or lab work" },
              { value: "0", label: "None of the above", exclusive: true },
            ],
          },
          {
            // Skip logic example 1: only shown if q5 has at least one real
            // item selected (i.e. "None of the above" was NOT chosen).
            id: "demo-q6",
            type: "nominal",
            stem: "Which of those was the most recent?",
            options: [
              { value: "1", label: "Annual check-up" },
              { value: "2", label: "Dental check-up" },
              { value: "3", label: "Eye exam" },
              { value: "4", label: "Vaccination or flu shot" },
              { value: "5", label: "Blood test or lab work" },
            ],
            showIf: { questionId: "demo-q5", excludesAny: ["0"] },
          },
        ],
      },
    ],
  },
  {
    id: "demo-module-2",
    kind: "standard",
    title: "Healthcare Access",
    sections: [
      {
        id: "demo-access-satisfaction",
        title: "Access & Satisfaction",
        questions: [
          {
            id: "demo-q7",
            type: "matrix",
            stem: "How would you rate the following aspects of your last healthcare visit?",
            items: [
              { id: "demo-q7-i0", label: "Wait time to see the doctor." },
              { id: "demo-q7-i1", label: "Clarity of the doctor's explanation." },
            ],
            options: [
              { value: "1", label: "Poor" },
              { value: "2", label: "Fair" },
              { value: "3", label: "Good" },
              { value: "4", label: "Very good" },
              { value: "5", label: "Excellent" },
            ],
          },
          {
            // Skip logic example 2: only shown if q4's answer was "No"
            // (no primary care doctor).
            id: "demo-q8",
            type: "nominal",
            stem: "What's the main reason you don't have a primary care doctor?",
            options: [
              { value: "1", label: "Cost" },
              { value: "2", label: "Lack of time" },
              { value: "3", label: "Haven't needed one" },
              { value: "4", label: "Don't know where to find one" },
              { value: "5", label: "Other" },
            ],
            showIf: { questionId: "demo-q4", equals: "0" },
          },
        ],
      },
      {
        id: "demo-wrapup",
        title: "Wrap-up",
        questions: [
          {
            id: "demo-q9",
            type: "ordinal",
            stem: "Overall, how satisfied are you with your access to healthcare?",
            options: [
              { value: "1", label: "Very dissatisfied" },
              { value: "2", label: "Dissatisfied" },
              { value: "3", label: "Neutral" },
              { value: "4", label: "Satisfied" },
              { value: "5", label: "Very satisfied" },
            ],
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