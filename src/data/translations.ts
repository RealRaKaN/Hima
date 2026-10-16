export type Lang = 'en' | 'ar';

export interface Translations {
  nav_live_demo: string;
  nav_validation: string;
  nav_target_species: string;
  nav_methodology: string;
  nav_hardware_setup: string;
  nav_contact: string;
  foot_whatsapp: string;
  nav_gallery: string;
  gallery_title: string;
  gallery_sub: string;
  gallery_empty: string;
  gallery_back: string;
  nav_academic_repo: string;
  hero_badge: string;
  hero_title: string;
  hero_subtitle: string;
  video_fullscreen: string;
  metric_map_title: string;
  metric_map_sub: string;
  metric_trk_title: string;
  metric_trk_sub: string;
  metric_fps_title: string;
  metric_fps_sub: string;
  metric_fpr_title: string;
  metric_fpr_sub: string;
  species_section_title: string;
  species_section_sub: string;
  oryx_title: string;
  gazelle_title: string;
  species_vulnerable: string;
  oryx_detail_1: string;
  oryx_detail_2: string;
  oryx_detail_3: string;
  gazelle_detail_1: string;
  gazelle_detail_2: string;
  gazelle_detail_3: string;
  arch_title: string;
  arch_sub: string;
  pipe1_title: string;
  pipe1_desc: string;
  pipe2_title: string;
  pipe2_desc: string;
  pipe3_title: string;
  pipe3_desc: string;
  pipe4_title: string;
  pipe4_desc: string;
  drone_3d_title: string;
  drone_3d_desc: string;
  tools_title: string;
  footer_title: string;
  footer_sub: string;
  foot_github: string;
  footer_copy: string;
}

export const translations: Record<Lang, Translations> = {
  en: {
    nav_live_demo: "Live Demo",
    nav_validation: "Validation",
    nav_target_species: "Target Species",
    nav_methodology: "Methodology",
    nav_hardware_setup: "Hardware Setup",
    nav_contact: "Contact us",
    foot_whatsapp: "WhatsApp",
    nav_gallery: "Gallery",
    gallery_title: "Gallery",
    gallery_sub: "Photos from the Hima project.",
    gallery_empty: "No images yet.",
    gallery_back: "Back to home",
    nav_academic_repo: "Academic Repository",
    hero_badge: "Qassim University — Computer Science Students",
    hero_title: "An Integrated Drone-Based System for Real-Time Wildlife Detection and Tracking Using Computer Vision",
    hero_subtitle: "Real-time AI detection, individual ID tracking, And automated guidance of the drone to monitor wildlife in the Arabian Desert.",
    video_fullscreen: "Full screen",
    metric_map_title: "mAP@0.50 Precision",
    metric_map_sub: " ",
    metric_trk_title: "Continuous Tracking",
    metric_trk_sub: "Remembers hidden animals more than 4 seconds",
    metric_fps_title: "Real-Time Inference FPS",
    metric_fps_sub: " ",
    metric_fpr_title: "False-Positive Rate",
    metric_fpr_sub: "Rarely confuses animals with desert rocks or bushes",
    species_section_title: "Target Species",
    species_section_sub: "Our system focuses on monitoring the key vulnerable species of the Arabian Peninsula ",
    oryx_title: "Arabian Oryx",
    gazelle_title: "Reem Gazelle",
    species_vulnerable: "Vulnerable",
    oryx_detail_1: "A medium-sized antelope with long straight horns",
    oryx_detail_2: "Habitat: Arabian Desert, Saudi Arabia",
    oryx_detail_3: "Key Features: Distinctive white coat, dark facial markings",
    gazelle_detail_1: "A species of gazelle native to Arabian Deserts",
    gazelle_detail_2: "Habitat: Sand dunes, open plains",
    gazelle_detail_3: "Key Features: Pale coat blending with sandy background",
    arch_title: "How It Works",
    arch_sub: "A simple overview of the system operation",
    pipe1_title: "Drone Flight",
    pipe1_desc: "The drone takes off and begins its patrol",
    pipe2_title: "Camera Activation",
    pipe2_desc: "The drone opens the camera to scan the environment",
    pipe3_title: "Animal Detection",
    pipe3_desc: "The AI model receives images and detects target animals",
    pipe4_title: "Behavior Analysis",
    pipe4_desc: "The system analyzes the situation and decides how to act",
    drone_3d_title: "Interactive 3D Drone Model",
    drone_3d_desc: "Explore the drone hardware setup interactively",
    tools_title: "Academic Stack & Toolchain",
    footer_title: "Why Hima ?, In authentic Arab heritage, the word 'Hima' means a safe haven and a natural reserve",
    footer_sub: "Computer Science Department, Qassim University",
    foot_github: "GitHub Repository",
    footer_copy: "Made By @RealRaKaN. All rights reserved."
  },
  ar: {
    nav_live_demo: "العرض المباشر",
    nav_validation: "مقاييس التحقق",
    nav_target_species: "الأنواع المستهدفة",
    nav_methodology: "المنهجية",
    nav_hardware_setup: "إعداد العتاد",
    nav_contact: "تواصل معنا",
    foot_whatsapp: "واتساب",
    nav_gallery: "معرض الصور",
    gallery_title: "معرض الصور",
    gallery_sub: "صور من مشروع حِمَى.",
    gallery_empty: "لا توجد صور بعد.",
    gallery_back: "العودة للرئيسية",
    nav_academic_repo: "المستودع الأكاديمي",
    hero_badge: "جامعة القصيم — طلاب علوم الحاسب",
    hero_title: "نظام متكامل قائم على الطائرات المسيّرة للكشف والتتبع الفوري للحياة البرية باستخدام الرؤية الحاسوبية ",
    hero_subtitle: "كشف بالذكاء الاصطناعي في الوقت الفعلي، تتبع للهوية الفردية، وتوجيه آلي للمسيّرة لرصد الحياة البرية في الصحراء العربية.",
    video_fullscreen: "ملء الشاشة",
    metric_map_title: "دقة mAP@0.50",
    metric_map_sub: " ",
    metric_trk_title: "استمرارية التتبع (إطارات)",
    metric_trk_sub: " النظام يتذكر الحيوان حتى لو اختفى خلف عائق لاكثر من 4 ثوانٍ ",
    metric_fps_title: "سرعة الاستدلال اللحظي (FPS)",
    metric_fps_sub: " ",
    metric_fpr_title: " نسبة الخطأ في التعرّف",
    metric_fpr_sub: " لا يخلط بين الحيوانات وصخور أو أشجار الصحراء",
    species_section_title: "الأنواع المستهدفة",
    species_section_sub: "يركز نظامنا على مراقبة الأنواع المهددة الرئيسية في شبه الجزيرة العربية",
    oryx_title: "المها العربي",
    gazelle_title: "غزال الريم",
    species_vulnerable: "مُهدد بالانقراض",
    oryx_detail_1: "ظبي متوسط الحجم ذو سنام مميز بكتفه وقرون طويلة مستقيمة",
    oryx_detail_2: "الموطن: الصحراء العربية، المملكة العربية السعودية",
    oryx_detail_3: "السمات المميزة: فراء أبيض ناصع، وعلامات وجهية داكنة",
    gazelle_detail_1: "نوع من الغزلان يستوطن الصحاري الرملية والسهول",
    gazelle_detail_2: "الموطن: الكثبان الرملية، السهول المفتوحة",
    gazelle_detail_3: "السمات المميزة: فراء شاحب يمتزج مع الطبيعة الرملية",
    arch_title: "آلية عمل النظام",
    arch_sub: "نظرة عامة مبسطة على كيفية تشغيل النظام وتكامله",
    pipe1_title: "تحليق الطائرة",
    pipe1_desc: "تقلع الطائرة المسيّرة وتبدأ مسار المراقبة",
    pipe2_title: "تفعيل الكاميرا",
    pipe2_desc: "يتم تفعيل الكاميرا لمسح البيئة المحيطة",
    pipe3_title: "اكتشاف الحيوانات",
    pipe3_desc: "يستقبل نموذج الذكاء الاصطناعي الصور ويكتشف الحيوانات المستهدفة",
    pipe4_title: "تحليل السلوك",
    pipe4_desc: "يحلل النظام الموقف ويتخذ القرار المناسب للتتبع",
    drone_3d_title: "نموذج 3D تفاعلي للمسيّرة",
    drone_3d_desc: "استكشف إعداد وعتاد الطائرة المسيّرة بشكل تفاعلي",
    tools_title: "الحزمة الأكاديمية وسلسلة الأدوات",
    footer_title: "لماذا حِمَى؟, في التراث العربي الأصيل، كلمة 'حِمَى' تعني الملاذ الآمن والمحمية الطبيعية. ",
    footer_sub: "قسم علوم الحاسب، جامعة القصيم",
    foot_github: "مستودع المشروع (GitHub)",
    footer_copy: "صنع بواسطة RealRaKaN@. جميع الحقوق محفوظة."
  }
};
