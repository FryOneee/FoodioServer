import openai
import json
import re

def query_meal_nutrients(image_url: str, user_context: dict):
    """
    Delegates the ChatGPT query to estimate macronutrient values based on the provided image URL
    and additional user context regarding dietary issues.
    The original query text is preserved.
    Returns a tuple containing:
      - A dictionary with keys: 'name', 'kcal', 'proteins', 'carbs', 'fats', 'healthy_index', 'problems'
      - The raw OpenAI result text
    """
    # Budujemy dodatkowy opis kontekstu użytkownika
    context_str = ""
    if user_context.get("diet"):
        context_str += f"Diet type: {user_context['diet']}. "
    if user_context.get("problems"):
        context_str += f"User problems: {', '.join(user_context['problems'])}. "

    prompt = (
        "Estimate the macronutrient values based on the image and the following user information: " +
        context_str +
        "Also, list potential issues with the meal (for example, dietary incompatibility, high fat content, or other problems) "
        "if applicable. Provide the result in JSON format, containing exactly the keys: 'name', 'kcal', 'proteins', 'carbs', 'fats', 'healthy_index', 'problems'. "
        "The value for 'problems' should be a list. Do not add any additional text."
    )

    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url}
                    }
                ]
            }
        ],
        max_tokens=300,
    )
    result_text = response.choices[0].message["content"]

    if result_text.startswith("```"):
        result_text = re.sub(r'^```(?:json)?\s*|```$', '', result_text).strip()

    # Jeśli wartość dla 'name' nie jest cytatem, dodajemy cudzysłowy
    fixed_text = re.sub(r'("name":\s*)([A-Za-z]+)', r'\1"\2"', result_text)
    try:
        parsed = json.loads(fixed_text)
        name_val = parsed.get("name", "dish")
        kcal_val = parsed.get("kcal", -1)
        proteins_val = parsed.get("proteins", -1)
        carbs_val = parsed.get("carbs", -1)
        fats_val = parsed.get("fats", -1)
        healthy_index_val = parsed.get("healthy_index", -1)
        problems_val = parsed.get("problems", [])
    except Exception as e:
        name_val = "dish"
        kcal_val = -1
        proteins_val = -1
        carbs_val = -1
        fats_val = -1
        healthy_index_val = -1
        problems_val = []

    return (
        {
            "name": name_val,
            "kcal": kcal_val,
            "proteins": proteins_val,
            "carbs": carbs_val,
            "fats": fats_val,
            "healthy_index": healthy_index_val,
            "problems": problems_val
        },
        result_text
    )




def new_goal(sex, birthDate, height, lifestyle, diet, startTime, endTime):
    # Convert shorthand gender to full string
    if sex == 'W':
        sex = "Women"
    elif sex == 'M':
        sex = "Men"

    # Build the prompt for the ChatGPT query
    prompt = (
        f"Based on the following data: Sex: {sex}, Birth Date: {birthDate}, Height: {height} cm, "
        f"Lifestyle: {lifestyle}, Diet: {diet}, Start Time: {startTime}, End Time: {endTime}. "
        "Please provide the daily recommended intake of calories (kcal), proteins, carbs, and fats for a person under these conditions. "
        "Respond in JSON format with exactly the keys: 'kcal', 'proteins', 'carbs', 'fats'. Do not include any additional text."
    )

    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "user", "content": prompt}
        ],
        max_tokens=150
    )

    result_text = response.choices[0].message["content"]

    try:
        parsed = json.loads(result_text)
        kcal = parsed.get("kcal", -1)
        proteins = parsed.get("proteins", -1)
        carbs = parsed.get("carbs", -1)
        fats = parsed.get("fats", -1)
    except Exception as e:
        kcal = proteins = carbs = fats = -1

    return {
        "kcal": kcal,
        "proteins": proteins,
        "carbs": carbs,
        "fats": fats
    }, result_text
