import os

# Solicitar al usuario que seleccione un archivo
archivo_seleccionado = input('Por favor, seleccione un archivo para leer: ')

# Verificar si el archivo existe
if os.path.exists(archivo_seleccionado):
    # Leer el contenido del archivo
    with open(archivo_seleccionado, 'r') as file:
        contenido = file.read()

    # Guardar el contenido en un archivo .txt
    nombre_archivo_txt = archivo_seleccionado.replace('.py', '.txt')
    with open(nombre_archivo_txt, 'w') as file:
        file.write(contenido)

    print(f'El contenido del archivo {archivo_seleccionado} ha sido guardado en {nombre_archivo_txt}')
else:
    print('El archivo seleccionado no existe.')