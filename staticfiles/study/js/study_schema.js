/* ==========================================================================
   study_schema.js — Module / Section / Question data model
   ----------------------------------------------------------------------
   PLACEHOLDER CONTENT. Structure is final, wording/items are not — swap in
   your real 3 standard + 16 elective modules by following the same shape.
   Only 3 standard + 3 example elective modules are fully written here to
   demonstrate the pattern; add the remaining 13 electives as more entries
   in STUDY_MODULES and list their ids in js/study_config.js.

   ORDERING (which modules run, and the order of modules/sections/questions)
   is controlled entirely by js/study_config.js — that's the single file to
   edit to reorder or enable/disable content. Nothing in this file needs to
   change to reorder things.

   SHAPE
   -----
   Module   { id, kind: "standard"|"elective", title, sections[], skipIf? }
   Section  { id, title, questions[], skipIf? }
   Question { id, type, stem, options[], showIf?, group? }

   type: "binary" | "ordinal" | "nominal" | "categorical"
     - binary       exactly 2 options (yes/no style), rendered as two
                    side-by-side choice cards.
     - ordinal      ordered options (low -> high). List them in that order;
                    the engine renders these as a connected horizontal
                    scale so order is visually obvious.
     - nominal      unordered categories, small closed set. Rendered as
                    stacked choice cards, same as categorical.
     - categorical  unordered categories, typically a broader/open set
                    (e.g. condition type, reason codes). Same rendering as
                    nominal; kept as a distinct type only so exports/
                    analysis scripts can tell the two apart if you need to.

   showIf   { questionId, equals } | { questionId, notEquals }
            | { questionId, in: [...] } | { questionId, notIn: [...] }
            Question is skipped entirely unless the referenced question
            (anywhere earlier in the study — ids are global) currently
            satisfies the condition. Omit for "always show".

   skipIf   Same condition shape, but on a Section (or Module): the whole
            section/module is skipped when the condition evaluates true.
            Omit for "never skip". This is the inverse polarity of showIf
            on purpose — "show if relevant" at question level reads
            naturally, "skip if irrelevant" at section/module level does
            too (e.g. skip a whole screening section once you already know
            it doesn't apply).

   group    Free-text tag with no engine behavior — purely a label so a
            cluster of branched follow-up questions can be recognized as
            belonging to the same branch when you're reading/exporting the
            schema. The UI shows a small "Follow-up" badge on any question
            that has a group set.
   ========================================================================== */

// NOTE: Module/section/question ORDERING now lives entirely in
// js/study_config.js (window.STUDY_CONFIG) — that's the one file to edit
// to change run order. This file only defines the content/branching.
window.STUDY_MODULES = [
  /* ====================================================================
     STANDARD MODULE 1
     ==================================================================== */
  {
    id: "std-general-health",
    kind: "standard",
    title: "General Health Background",
    sections: [
      {
        id: "sec-demographics-access",
        title: "Demographics & Access",
        questions: [
          {
            id: "q-age-range",
            type: "nominal",
            stem: "Which age range do you fall into?",
            options: [
              { value: "18-29", label: "18–29" },
              { value: "30-44", label: "30–44" },
              { value: "45-59", label: "45–59" },
              { value: "60+", label: "60 or older" }
            ]
          },
          {
            id: "q-health-insurance",
            type: "binary",
            stem: "Do you currently have health insurance coverage?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          }
        ]
      },
      {
        id: "sec-medical-history",
        title: "Medical History",
        questions: [
          {
            id: "q-visited-doctor",
            type: "binary",
            stem: "Have you visited a doctor or clinic in the past 6 months?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-visit-reason",
            type: "nominal",
            stem: "What was the main reason for that visit?",
            options: [
              { value: "routine", label: "Routine check-up" },
              { value: "illness", label: "Acute illness" },
              { value: "chronic", label: "Chronic condition follow-up" },
              { value: "other", label: "Other" }
            ],
            showIf: { questionId: "q-visited-doctor", equals: "yes" },
            group: "doctor-visit-followup"
          },
          {
            id: "q-visit-satisfaction",
            type: "ordinal",
            stem: "How satisfied were you with that visit?",
            options: [
              { value: "very-dissatisfied", label: "Very dissatisfied" },
              { value: "dissatisfied", label: "Dissatisfied" },
              { value: "neutral", label: "Neutral" },
              { value: "satisfied", label: "Satisfied" },
              { value: "very-satisfied", label: "Very satisfied" }
            ],
            showIf: { questionId: "q-visited-doctor", equals: "yes" },
            group: "doctor-visit-followup"
          }
        ]
      },
      {
        id: "sec-chronic-conditions",
        title: "Chronic Conditions",
        questions: [
          {
            id: "q-chronic-condition",
            type: "binary",
            stem: "Do you have a diagnosed chronic condition?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-condition-type",
            type: "categorical",
            stem: "Which category best describes that condition?",
            options: [
              { value: "cardio", label: "Cardiovascular" },
              { value: "metabolic", label: "Metabolic (e.g. diabetes)" },
              { value: "respiratory", label: "Respiratory" },
              { value: "musculoskeletal", label: "Musculoskeletal" },
              { value: "other", label: "Other" }
            ],
            showIf: { questionId: "q-chronic-condition", equals: "yes" },
            group: "chronic-condition-followup"
          },
          {
            id: "q-condition-management",
            type: "ordinal",
            stem: "How well would you say that condition is currently managed?",
            options: [
              { value: "not-well", label: "Not well" },
              { value: "somewhat", label: "Somewhat well" },
              { value: "well", label: "Well" },
              { value: "very-well", label: "Very well" }
            ],
            showIf: { questionId: "q-chronic-condition", equals: "yes" },
            group: "chronic-condition-followup"
          }
        ]
      },
      {
        id: "sec-family-history",
        title: "Family History",
        questions: [
          {
            id: "q-family-diabetes-heart",
            type: "binary",
            stem: "Do you have a family history of diabetes or heart disease?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          }
        ]
      },
      {
        id: "sec-lifestyle-snapshot",
        title: "Lifestyle Snapshot",
        questions: [
          {
            id: "q-physical-activity",
            type: "ordinal",
            stem: "How often do you engage in moderate physical activity (e.g. brisk walking) per week?",
            options: [
              { value: "none", label: "Not at all" },
              { value: "1-2", label: "1–2 times" },
              { value: "3-4", label: "3–4 times" },
              { value: "5+", label: "5 or more times" }
            ]
          },
          {
            id: "q-smoking",
            type: "binary",
            stem: "Do you currently smoke or use tobacco products?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-sleep-hours",
            type: "ordinal",
            stem: "On average, how many hours of sleep do you get per night?",
            options: [
              { value: "lt5", label: "Less than 5 hours" },
              { value: "5-6", label: "5–6 hours" },
              { value: "7-8", label: "7–8 hours" },
              { value: "gt8", label: "More than 8 hours" }
            ]
          }
        ]
      }
    ]
  },

  /* ====================================================================
     STANDARD MODULE 2
     ==================================================================== */
  {
    id: "std-access-navigation",
    kind: "standard",
    title: "Healthcare Access & Navigation",
    sections: [
      {
        id: "sec-access",
        title: "Access",
        questions: [
          {
            id: "q-access-ease",
            type: "ordinal",
            stem: "How easy is it for you to access healthcare services when needed?",
            options: [
              { value: "very-easy", label: "Very easy" },
              { value: "somewhat-easy", label: "Somewhat easy" },
              { value: "difficult", label: "Difficult" },
              { value: "very-difficult", label: "Very difficult" }
            ]
          },
          {
            id: "q-access-barrier",
            type: "categorical",
            stem: "What's the main barrier that makes it difficult?",
            options: [
              { value: "distance", label: "Distance / transport" },
              { value: "cost", label: "Cost" },
              { value: "wait-times", label: "Wait times" },
              { value: "availability", label: "Provider availability" },
              { value: "other", label: "Other" }
            ],
            showIf: { questionId: "q-access-ease", in: ["difficult", "very-difficult"] },
            group: "access-barrier-followup"
          }
        ]
      },
      {
        id: "sec-insurance-cost",
        title: "Insurance & Cost",
        questions: [
          {
            id: "q-cost-worry",
            type: "ordinal",
            stem: "How often do you worry about the cost of healthcare?",
            options: [
              { value: "never", label: "Never" },
              { value: "rarely", label: "Rarely" },
              { value: "sometimes", label: "Sometimes" },
              { value: "often", label: "Often" }
            ]
          },
          {
            id: "q-delayed-care-cost",
            type: "binary",
            stem: "Have you ever delayed or skipped care because of cost?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-delay-reason",
            type: "nominal",
            stem: "What kind of care did you delay or skip?",
            options: [
              { value: "checkup", label: "Routine check-up" },
              { value: "specialist", label: "Specialist visit" },
              { value: "medication", label: "Medication" },
              { value: "procedure", label: "Procedure / test" }
            ],
            showIf: { questionId: "q-delayed-care-cost", equals: "yes" },
            group: "delayed-care-followup"
          }
        ]
      },
      {
        id: "sec-health-literacy",
        title: "Health Literacy",
        questions: [
          {
            id: "q-understand-medical-info",
            type: "ordinal",
            stem: "How confident are you in understanding medical information given by a doctor?",
            options: [
              { value: "not-confident", label: "Not confident" },
              { value: "somewhat-confident", label: "Somewhat confident" },
              { value: "confident", label: "Confident" },
              { value: "very-confident", label: "Very confident" }
            ]
          },
          {
            id: "q-reads-labels",
            type: "binary",
            stem: "Do you typically read medication labels/instructions in full?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          }
        ]
      },
      {
        id: "sec-preventive-care",
        title: "Preventive Care",
        questions: [
          {
            id: "q-annual-checkup",
            type: "binary",
            stem: "Have you received an annual health check-up in the past year?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-checkup-skip-reason",
            type: "categorical",
            stem: "What's the main reason you haven't had one?",
            options: [
              { value: "no-time", label: "No time" },
              { value: "cost", label: "Cost" },
              { value: "no-symptoms", label: "No symptoms, didn't feel necessary" },
              { value: "access", label: "Couldn't get an appointment" },
              { value: "other", label: "Other" }
            ],
            showIf: { questionId: "q-annual-checkup", equals: "no" },
            group: "checkup-skip-followup"
          }
        ]
      },
      {
        id: "sec-provider-relationship",
        title: "Provider Relationship",
        questions: [
          {
            id: "q-has-regular-provider",
            type: "binary",
            stem: "Do you have a regular doctor or clinic you go to?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-provider-trust",
            type: "ordinal",
            stem: "How much do you trust that provider's advice?",
            options: [
              { value: "low", label: "Low" },
              { value: "moderate", label: "Moderate" },
              { value: "high", label: "High" },
              { value: "very-high", label: "Very high" }
            ],
            showIf: { questionId: "q-has-regular-provider", equals: "yes" },
            group: "provider-trust-followup"
          }
        ]
      },
      {
        id: "sec-telehealth",
        title: "Telehealth",
        questions: [
          {
            id: "q-telehealth-used",
            type: "binary",
            stem: "Have you used a telehealth (video/phone) appointment in the past year?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-telehealth-satisfaction",
            type: "ordinal",
            stem: "How satisfied were you with that telehealth experience?",
            options: [
              { value: "very-dissatisfied", label: "Very dissatisfied" },
              { value: "dissatisfied", label: "Dissatisfied" },
              { value: "neutral", label: "Neutral" },
              { value: "satisfied", label: "Satisfied" },
              { value: "very-satisfied", label: "Very satisfied" }
            ],
            showIf: { questionId: "q-telehealth-used", equals: "yes" },
            group: "telehealth-followup"
          }
        ]
      }
    ]
  },

  /* ====================================================================
     STANDARD MODULE 3
     ==================================================================== */
  {
    id: "std-wellbeing-baseline",
    kind: "standard",
    title: "Wellbeing Baseline",
    sections: [
      {
        id: "sec-mood",
        title: "Mood",
        questions: [
          {
            id: "q-mood-today",
            type: "ordinal",
            stem: "Overall, how would you describe your mood today?",
            options: [
              { value: "very-low", label: "Very low" },
              { value: "low", label: "Low" },
              { value: "neutral", label: "Neutral" },
              { value: "good", label: "Good" },
              { value: "very-good", label: "Very good" }
            ]
          }
        ]
      },
      {
        id: "sec-stress",
        title: "Stress",
        questions: [
          {
            id: "q-stress-level",
            type: "ordinal",
            stem: "How would you describe your typical stress level over the past month?",
            options: [
              { value: "low", label: "Low" },
              { value: "moderate", label: "Moderate" },
              { value: "high", label: "High" },
              { value: "very-high", label: "Very high" }
            ]
          },
          {
            id: "q-stress-source",
            type: "nominal",
            stem: "What's the main source of that stress?",
            options: [
              { value: "work", label: "Work / study" },
              { value: "finances", label: "Finances" },
              { value: "health", label: "Health" },
              { value: "relationships", label: "Relationships" },
              { value: "other", label: "Other" }
            ],
            showIf: { questionId: "q-stress-level", in: ["high", "very-high"] },
            group: "stress-source-followup"
          }
        ]
      },
      {
        id: "sec-sleep-quality",
        title: "Sleep Quality",
        questions: [
          {
            id: "q-sleep-quality",
            type: "ordinal",
            stem: "Overall, how would you rate your sleep quality lately?",
            options: [
              { value: "very-poor", label: "Very poor" },
              { value: "poor", label: "Poor" },
              { value: "fair", label: "Fair" },
              { value: "good", label: "Good" },
              { value: "very-good", label: "Very good" }
            ]
          },
          {
            id: "q-sleep-issue-type",
            type: "categorical",
            stem: "What's the main issue affecting your sleep?",
            options: [
              { value: "falling-asleep", label: "Trouble falling asleep" },
              { value: "staying-asleep", label: "Waking up during the night" },
              { value: "waking-early", label: "Waking up too early" },
              { value: "not-restful", label: "Sleep doesn't feel restful" }
            ],
            showIf: { questionId: "q-sleep-quality", in: ["very-poor", "poor"] },
            group: "sleep-issue-followup"
          }
        ]
      },
      {
        id: "sec-social-support",
        title: "Social Support",
        questions: [
          {
            id: "q-social-support",
            type: "ordinal",
            stem: "How supported do you feel by people around you right now?",
            options: [
              { value: "not-supported", label: "Not supported" },
              { value: "somewhat", label: "Somewhat supported" },
              { value: "supported", label: "Supported" },
              { value: "very-supported", label: "Very supported" }
            ]
          }
        ]
      },
      {
        id: "sec-physical-symptoms",
        title: "Physical Symptoms",
        questions: [
          {
            id: "q-recent-symptoms",
            type: "binary",
            stem: "Have you experienced any new physical symptoms in the past 2 weeks?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-symptom-severity",
            type: "ordinal",
            stem: "How would you rate the severity of those symptoms?",
            options: [
              { value: "mild", label: "Mild" },
              { value: "moderate", label: "Moderate" },
              { value: "severe", label: "Severe" }
            ],
            showIf: { questionId: "q-recent-symptoms", equals: "yes" },
            group: "symptom-followup"
          }
        ]
      }
    ]
  },

  /* ====================================================================
     ELECTIVE MODULE — example 1 of 16
     ==================================================================== */
  {
    id: "elec-diabetes-risk",
    kind: "elective",
    title: "Diabetes Risk Screening",
    sections: [
      {
        id: "sec-risk-factors",
        title: "Risk Factors",
        questions: [
          {
            id: "q-bmi-category",
            type: "categorical",
            stem: "Which weight category best describes you (self-reported)?",
            options: [
              { value: "underweight", label: "Underweight" },
              { value: "normal", label: "Normal range" },
              { value: "overweight", label: "Overweight" },
              { value: "obese", label: "Obese" },
              { value: "prefer-not-say", label: "Prefer not to say" }
            ]
          }
        ]
      },
      {
        id: "sec-symptoms",
        title: "Symptoms",
        questions: [
          {
            id: "q-thirst-frequency",
            type: "ordinal",
            stem: "How often do you feel unusually thirsty?",
            options: [
              { value: "never", label: "Never" },
              { value: "rarely", label: "Rarely" },
              { value: "often", label: "Often" },
              { value: "constantly", label: "Constantly" }
            ]
          },
          {
            id: "q-frequent-urination",
            type: "binary",
            stem: "Have you noticed needing to urinate more frequently than usual?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          }
        ]
      },
      {
        id: "sec-monitoring",
        title: "Monitoring",
        questions: [
          {
            id: "q-blood-sugar-checked",
            type: "binary",
            stem: "Have you had your blood sugar checked in the past year?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-last-checked-when",
            type: "nominal",
            stem: "When was it last checked?",
            options: [
              { value: "lt3m", label: "Within the last 3 months" },
              { value: "3-6m", label: "3–6 months ago" },
              { value: "6-12m", label: "6–12 months ago" }
            ],
            showIf: { questionId: "q-blood-sugar-checked", equals: "yes" },
            group: "blood-sugar-followup"
          }
        ]
      },
      {
        id: "sec-diet",
        title: "Diet",
        questions: [
          {
            id: "q-sugary-drink-frequency",
            type: "ordinal",
            stem: "How often do you drink sugary beverages?",
            options: [
              { value: "never", label: "Never" },
              { value: "few-times-week", label: "A few times a week" },
              { value: "daily", label: "Daily" },
              { value: "multiple-daily", label: "Multiple times a day" }
            ]
          }
        ]
      },
      {
        id: "sec-management",
        title: "Management",
        questions: [
          {
            id: "q-diagnosed-diabetes",
            type: "binary",
            stem: "Have you been diagnosed with diabetes?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-diabetes-type",
            type: "categorical",
            stem: "Which type?",
            options: [
              { value: "type1", label: "Type 1" },
              { value: "type2", label: "Type 2" },
              { value: "gestational", label: "Gestational" },
              { value: "other", label: "Other / not sure" }
            ],
            showIf: { questionId: "q-diagnosed-diabetes", equals: "yes" },
            group: "diabetes-diagnosis-followup"
          },
          {
            id: "q-medication-adherence",
            type: "ordinal",
            stem: "How consistently do you take your prescribed medication/insulin as directed?",
            options: [
              { value: "rarely", label: "Rarely" },
              { value: "sometimes", label: "Sometimes" },
              { value: "usually", label: "Usually" },
              { value: "always", label: "Always" }
            ],
            showIf: { questionId: "q-diagnosed-diabetes", equals: "yes" },
            group: "diabetes-diagnosis-followup"
          }
        ]
      }
    ]
  },

  /* ====================================================================
     ELECTIVE MODULE — example 2 of 16 (demonstrates section-level skipIf)
     ==================================================================== */
  {
    id: "elec-sleep-quality",
    kind: "elective",
    title: "Sleep Quality Deep-Dive",
    sections: [
      {
        id: "sec-sleep-patterns",
        title: "Sleep Patterns",
        questions: [
          {
            id: "q-elec-sleep-quality-rating",
            type: "ordinal",
            stem: "Taking everything into account, how would you rate your sleep quality overall?",
            options: [
              { value: "excellent", label: "Excellent" },
              { value: "good", label: "Good" },
              { value: "fair", label: "Fair" },
              { value: "poor", label: "Poor" }
            ]
          },
          {
            id: "q-bedtime-consistency",
            type: "ordinal",
            stem: "How consistent is your bedtime from night to night?",
            options: [
              { value: "very-inconsistent", label: "Very inconsistent" },
              { value: "somewhat-inconsistent", label: "Somewhat inconsistent" },
              { value: "consistent", label: "Consistent" },
              { value: "very-consistent", label: "Very consistent" }
            ]
          }
        ]
      },
      {
        id: "sec-sleep-environment",
        title: "Sleep Environment",
        questions: [
          {
            id: "q-noise-disturbance",
            type: "binary",
            stem: "Is your sleep regularly disturbed by noise or light?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-disturbance-source",
            type: "nominal",
            stem: "What's the main source?",
            options: [
              { value: "traffic", label: "Traffic / outside noise" },
              { value: "household", label: "Household members" },
              { value: "devices", label: "Devices / screens" },
              { value: "light", label: "Light" }
            ],
            showIf: { questionId: "q-noise-disturbance", equals: "yes" },
            group: "disturbance-followup"
          }
        ]
      },
      {
        id: "sec-daytime-impact",
        title: "Daytime Impact",
        questions: [
          {
            id: "q-daytime-sleepiness",
            type: "ordinal",
            stem: "How often do you feel excessively sleepy during the day?",
            options: [
              { value: "never", label: "Never" },
              { value: "rarely", label: "Rarely" },
              { value: "often", label: "Often" },
              { value: "daily", label: "Daily" }
            ]
          }
        ]
      },
      {
        id: "sec-sleep-aids",
        title: "Sleep Aids",
        questions: [
          {
            id: "q-uses-sleep-aid",
            type: "binary",
            stem: "Do you use anything (medication, supplements, etc.) to help you sleep?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-sleep-aid-type",
            type: "categorical",
            stem: "What type?",
            options: [
              { value: "otc", label: "Over-the-counter" },
              { value: "prescription", label: "Prescription" },
              { value: "supplement", label: "Supplement (e.g. melatonin)" },
              { value: "other", label: "Other" }
            ],
            showIf: { questionId: "q-uses-sleep-aid", equals: "yes" },
            group: "sleep-aid-followup"
          }
        ]
      },
      {
        id: "sec-sleep-disorders",
        title: "Sleep Disorders Screening",
        // Example of a section-level skip: participants who already rated
        // their sleep as "excellent" skip the disorder-screening section
        // entirely, rather than being asked two more irrelevant questions.
        skipIf: { questionId: "q-elec-sleep-quality-rating", equals: "excellent" },
        questions: [
          {
            id: "q-snoring-reported",
            type: "binary",
            stem: "Has anyone told you that you snore loudly or gasp during sleep?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-diagnosed-sleep-disorder",
            type: "binary",
            stem: "Have you been diagnosed with a sleep disorder (e.g. sleep apnea, insomnia)?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          }
        ]
      }
    ]
  },

  /* ====================================================================
     ELECTIVE MODULE — example 3 of 16
     Placeholder wording only — not drawn from any published/copyrighted
     scale. Swap in your validated items when ready.
     ==================================================================== */
  {
    id: "elec-mental-health-screen",
    kind: "elective",
    title: "Mental Health Screen",
    sections: [
      {
        id: "sec-mood-patterns",
        title: "Mood Patterns",
        questions: [
          {
            id: "q-low-mood-frequency",
            type: "ordinal",
            stem: "Over the past two weeks, how often have you felt down or low?",
            options: [
              { value: "not-at-all", label: "Not at all" },
              { value: "several-days", label: "Several days" },
              { value: "more-than-half", label: "More than half the days" },
              { value: "nearly-every-day", label: "Nearly every day" }
            ]
          }
        ]
      },
      {
        id: "sec-anxiety-patterns",
        title: "Anxiety Patterns",
        questions: [
          {
            id: "q-worry-frequency",
            type: "ordinal",
            stem: "Over the past two weeks, how often have you felt unable to stop or control worrying?",
            options: [
              { value: "not-at-all", label: "Not at all" },
              { value: "several-days", label: "Several days" },
              { value: "more-than-half", label: "More than half the days" },
              { value: "nearly-every-day", label: "Nearly every day" }
            ]
          }
        ]
      },
      {
        id: "sec-energy-motivation",
        title: "Energy & Motivation",
        questions: [
          {
            id: "q-energy-level",
            type: "ordinal",
            stem: "How would you rate your energy level over the past two weeks?",
            options: [
              { value: "very-low", label: "Very low" },
              { value: "low", label: "Low" },
              { value: "moderate", label: "Moderate" },
              { value: "high", label: "High" }
            ]
          }
        ]
      },
      {
        id: "sec-coping",
        title: "Coping",
        questions: [
          {
            id: "q-coping-strategy",
            type: "nominal",
            stem: "Which of these best describes how you usually cope with stress?",
            options: [
              { value: "talk-someone", label: "Talking to someone" },
              { value: "physical-activity", label: "Physical activity" },
              { value: "distraction", label: "Distraction (media, hobbies)" },
              { value: "avoidance", label: "Avoiding the situation" },
              { value: "other", label: "Other" }
            ]
          }
        ]
      },
      {
        id: "sec-support-seeking",
        title: "Support Seeking",
        questions: [
          {
            id: "q-sought-support",
            type: "binary",
            stem: "In the past year, have you sought support for your mental health?",
            options: [
              { value: "yes", label: "Yes" },
              { value: "no", label: "No" }
            ]
          },
          {
            id: "q-support-type",
            type: "categorical",
            stem: "What type of support?",
            options: [
              { value: "friends-family", label: "Friends / family" },
              { value: "counselor-therapist", label: "Counselor / therapist" },
              { value: "doctor", label: "Doctor" },
              { value: "helpline", label: "Helpline / online resource" },
              { value: "other", label: "Other" }
            ],
            showIf: { questionId: "q-sought-support", equals: "yes" },
            group: "support-seeking-followup"
          }
        ]
      }
    ]
  }

  /* ---------------------------------------------------------------------
     Add the remaining 13 elective modules here, following the exact same
     shape: { id, kind: "elective", title, sections: [ { id, title,
     questions: [ {id, type, stem, options, showIf?, group?} ] } ] }
     Then add each new module's id to STUDY_CONFIG.activeModuleIds above.
     --------------------------------------------------------------------- */
];