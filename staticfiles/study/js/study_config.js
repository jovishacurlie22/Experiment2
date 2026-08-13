/* ==========================================================================
   study_config.js — Single source of truth for ORDERING
   ----------------------------------------------------------------------
   This is the ONE file to edit to change which modules run and in what
   order modules / sections / questions are presented. Nothing in
   study_schema.js needs to change to reorder the study.

   moduleOrder     Which modules run, and in what order, for this session.
                    Array of module ids from study_schema.js. Every
                    participant gets the standard modules; swap/extend the
                    elective ids with whatever subset is assigned to this
                    participant (e.g. injected from your Django context
                    before this script loads). A module id left out of this
                    list simply never runs.

   sectionOrder     { moduleId: [sectionId, sectionId, ...] }
                    Optional per-module override of section order. If a
                    module id has no entry here, sections run in the order
                    they're written in study_schema.js. Unknown ids are
                    silently dropped; sections missing from the list are
                    silently dropped too — once a module has an entry here,
                    it's treated as authoritative for that module.

   questionOrder    { sectionId: [questionId, questionId, ...] }
                    Same idea, one level down: optional per-section override
                    of question order within that section. Same fallback
                    (schema array order) and same authoritative-once-present
                    rule as sectionOrder.

   Both maps are optional and can be partial — only list the
   modules/sections you actually want to reorder; everything else falls
   back to the order things are written in study_schema.js.
   ========================================================================== */

window.STUDY_CONFIG = {
  activeModuleIds: [
    "std-general-health",
    "std-access-navigation",
    "std-wellbeing-baseline",
    "elec-diabetes-risk",
    "elec-sleep-quality",
    "elec-mental-health-screen"
  ],

  sectionOrder: {
    "std-general-health": [
      "sec-demographics-access",
      "sec-lifestyle-snapshot",
      "sec-medical-history",
      "sec-chronic-conditions",
      "sec-family-history"
    ],
    "std-access-navigation": [
      "sec-access",
      "sec-insurance-cost",
      "sec-health-literacy",
      "sec-preventive-care",
      "sec-provider-relationship",
      "sec-telehealth"
    ],
    "std-wellbeing-baseline": [
      "sec-mood",
      "sec-stress",
      "sec-sleep-quality",
      "sec-social-support",
      "sec-physical-symptoms"
    ],
    "elec-diabetes-risk": [
      "sec-risk-factors",
      "sec-symptoms",
      "sec-monitoring",
      "sec-diet",
      "sec-management"
    ],
    "elec-sleep-quality": [
      "sec-sleep-patterns",
      "sec-sleep-environment",
      "sec-daytime-impact",
      "sec-sleep-aids",
      "sec-sleep-disorders"
    ],
    "elec-mental-health-screen": [
      "sec-mood-patterns",
      "sec-anxiety-patterns",
      "sec-energy-motivation",
      "sec-coping",
      "sec-support-seeking"
    ]
  },

  questionOrder: {
    // Example: lifestyle questions asked sleep-hours first, since sleep is
    // also the focus of a later elective module.
    "sec-lifestyle-snapshot": ["q-sleep-hours", "q-physical-activity", "q-smoking"]
    // Add more `sectionId: [questionId, ...]` entries here as needed —
    // any section left out just keeps the order it's written in
    // study_schema.js.
  }
};
