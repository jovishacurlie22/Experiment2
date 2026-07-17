/* ==========================================================================
   Placeholder healthcare survey questions.
   Replace this array with the finalized question set — no other file needs
   to change. Each item:
     id:        unique string, used in stored responses
     type:      "mcq" | "binary"
     stem:      question text shown to participant
     options:   array of { value, label } — for binary keep exactly 2 options
   ========================================================================== */

window.STUDY_QUESTIONS = [
  {
    id: "q01",
    type: "binary",
    stem: "Have you visited a doctor or clinic in the past 6 months?",
    options: [
      { value: "yes", label: "Yes" },
      { value: "no", label: "No" }
    ]
  },
  {
    id: "q02",
    type: "mcq",
    stem: "How would you rate your overall health today?",
    options: [
      { value: "excellent", label: "Excellent" },
      { value: "good", label: "Good" },
      { value: "fair", label: "Fair" },
      { value: "poor", label: "Poor" }
    ]
  },
  {
    id: "q03",
    type: "binary",
    stem: "Do you currently have health insurance coverage?",
    options: [
      { value: "yes", label: "Yes" },
      { value: "no", label: "No" }
    ]
  },
  {
    id: "q04",
    type: "mcq",
    stem: "How often do you engage in moderate physical activity (e.g. brisk walking) per week?",
    options: [
      { value: "none", label: "Not at all" },
      { value: "1-2", label: "1–2 times" },
      { value: "3-4", label: "3–4 times" },
      { value: "5+", label: "5 or more times" }
    ]
  },
  {
    id: "q05",
    type: "binary",
    stem: "Do you currently smoke or use tobacco products?",
    options: [
      { value: "yes", label: "Yes" },
      { value: "no", label: "No" }
    ]
  },
  {
    id: "q06",
    type: "mcq",
    stem: "On average, how many hours of sleep do you get per night?",
    options: [
      { value: "lt5", label: "Less than 5 hours" },
      { value: "5-6", label: "5–6 hours" },
      { value: "7-8", label: "7–8 hours" },
      { value: "gt8", label: "More than 8 hours" }
    ]
  },
  {
    id: "q07",
    type: "binary",
    stem: "Are you currently taking any prescribed medication on a regular basis?",
    options: [
      { value: "yes", label: "Yes" },
      { value: "no", label: "No" }
    ]
  },
  {
    id: "q08",
    type: "mcq",
    stem: "How would you describe your typical stress level over the past month?",
    options: [
      { value: "low", label: "Low" },
      { value: "moderate", label: "Moderate" },
      { value: "high", label: "High" },
      { value: "very-high", label: "Very high" }
    ]
  },
  {
    id: "q09",
    type: "binary",
    stem: "Do you have a family history of diabetes or heart disease?",
    options: [
      { value: "yes", label: "Yes" },
      { value: "no", label: "No" }
    ]
  },
  {
    id: "q10",
    type: "mcq",
    stem: "How easy is it for you to access healthcare services when needed?",
    options: [
      { value: "very-easy", label: "Very easy" },
      { value: "somewhat-easy", label: "Somewhat easy" },
      { value: "difficult", label: "Difficult" },
      { value: "very-difficult", label: "Very difficult" }
    ]
  },
  {
    id: "q11",
    type: "binary",
    stem: "Have you received an annual health check-up in the past year?",
    options: [
      { value: "yes", label: "Yes" },
      { value: "no", label: "No" }
    ]
  },
  {
    id: "q12",
    type: "mcq",
    stem: "How confident are you in understanding medical information given by a doctor?",
    options: [
      { value: "very-confident", label: "Very confident" },
      { value: "confident", label: "Confident" },
      { value: "somewhat-confident", label: "Somewhat confident" },
      { value: "not-confident", label: "Not confident" }
    ]
  }
];
