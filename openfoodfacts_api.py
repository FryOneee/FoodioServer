import requests




def getInfoFromOpenFoodsApi(barcode):
    try:
        # Build the URL with the provided barcode
        json_file = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"

        # Pobranie danych z URL
        response = requests.get(json_file)
        response.raise_for_status()  # Raise an error for bad status codes
        data = response.json()
    except Exception as e:
        print("Wystąpił błąd podczas pobierania danych:", e)
        return None

    # Pobranie informacji o produkcie
    product = data.get("product", {})
    name = product.get("product_name", "No name")

    nutriments = product.get("nutriments", {})
    kcal = nutriments.get("energy-kcal_serving", 0)
    proteins = nutriments.get("proteins_serving", 0)
    carbs = nutriments.get("carbohydrates_serving", 0)
    fats = nutriments.get("fat_serving", 0)

    sklad = product.get("ingredients_text", [])
    image_front_url = product.get("image_front_url", "no image")

    # Wyświetlenie danych
    # print("Nazwa:", name)
    # print("Kcal:", kcal)
    # print("Proteins:", proteins)
    # print("Carbs:", carbs)
    # print("Fats:", fats)
    # print("Skład:", sklad)
    # print("Image Front URL:", image_front_url)

    return name, kcal, proteins, carbs, fats, sklad, image_front_url


if __name__ == '__main__':
    getInfoFromOpenFoodsApi("5000112651324")
    print()
    getInfoFromOpenFoodsApi("5901939103372")