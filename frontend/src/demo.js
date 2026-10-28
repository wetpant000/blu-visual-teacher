// A short silent lesson rendered live on the home page. It switches language with the language picker.
const TEXT = {
  en: ["How does the water cycle work?", ["Evaporation", "Clouds form", "Rain falls", "Water collects"],
    "The Sun warms water, so it rises as vapour. It cools into clouds and falls back as rain."],
  hi: ["जल चक्र कैसे काम करता है?", ["वाष्पीकरण", "बादल बनते हैं", "बारिश होती है", "पानी जमा होता है"],
    "सूरज पानी को गर्म करता है और वह भाप बनकर ऊपर उठता है। ठंडा होकर वह बादल बनता है और बारिश बनकर गिरता है।"],
  kn: ["ಜಲಚಕ್ರ ಹೇಗೆ ಕೆಲಸ ಮಾಡುತ್ತದೆ?", ["ಆವಿಯಾಗುವಿಕೆ", "ಮೋಡಗಳು ಆಗುತ್ತವೆ", "ಮಳೆ ಬೀಳುತ್ತದೆ", "ನೀರು ಸೇರುತ್ತದೆ"],
    "ಸೂರ್ಯನು ನೀರನ್ನು ಬಿಸಿಮಾಡುತ್ತಾನೆ, ಅದು ಆವಿಯಾಗಿ ಮೇಲೇರುತ್ತದೆ. ತಂಪಾಗಿ ಮೋಡವಾಗಿ ಮಳೆಯಾಗಿ ಬೀಳುತ್ತದೆ."],
  ta: ["நீர் சுழற்சி எப்படி நடக்கிறது?", ["ஆவியாதல்", "மேகங்கள் உருவாகும்", "மழை பெய்யும்", "நீர் சேரும்"],
    "சூரியன் நீரை சூடாக்குகிறது, அது ஆவியாக மேலே எழுகிறது. குளிர்ந்து மேகமாகி மழையாகப் பெய்கிறது."],
  te: ["నీటి చక్రం ఎలా పని చేస్తుంది?", ["బాష్పీభవనం", "మేఘాలు ఏర్పడతాయి", "వర్షం కురుస్తుంది", "నీరు చేరుతుంది"],
    "సూర్యుడు నీటిని వేడి చేస్తాడు, అది ఆవిరిగా పైకి లేస్తుంది. చల్లబడి మేఘంగా మారి వర్షంగా కురుస్తుంది."],
  bn: ["জলচক্র কীভাবে কাজ করে?", ["বাষ্পীভবন", "মেঘ তৈরি হয়", "বৃষ্টি পড়ে", "জল জমা হয়"],
    "সূর্য জল গরম করে, তা বাষ্প হয়ে ওপরে ওঠে। ঠান্ডা হয়ে মেঘ হয় আর বৃষ্টি হয়ে নেমে আসে।"],
  mr: ["जलचक्र कसे काम करते?", ["बाष्पीभवन", "ढग तयार होतात", "पाऊस पडतो", "पाणी साठते"],
    "सूर्य पाणी तापवतो आणि ते वाफ होऊन वर जाते. थंड होऊन ढग बनतात आणि पाऊस पडतो."],
};

export const DEMO_ROLES = ["teacher", "explorer", "scientist", "astronaut", "engineer", "robot", "diver"];

export function demoLesson(lang, round = 0) {
  const [title, items, narration] = TEXT[lang] || TEXT.en;
  return {
    title,
    language: TEXT[lang] ? lang : "en",
    theme: {
      id: (round * 37).toString(16).padStart(2, "0"),
      archetype: "ocean",
      character: DEMO_ROLES[round % DEMO_ROLES.length],
      motion: round % 4,
      palette: {
        background: "hsl(196 45% 10%)", surface: "hsl(196 40% 17%)", grid: "hsl(196 35% 28%)",
        accent: "hsl(42 95% 62%)", secondary: "hsl(176 70% 62%)", glow: "hsl(196 70% 28%)",
        character: "hsl(214 62% 52%)", characterShade: "hsl(214 60% 36%)",
      },
    },
    scenes: [
      { type: "title_card", heading: title, narration: title, items: [], audio_seconds: 3.2, duration: 3.8 },
      { type: "diagram", diagram_kind: "cycle", heading: title, narration, items, audio_seconds: 9, duration: 10.5 },
    ],
  };
}
