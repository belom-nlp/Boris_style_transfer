# -*- coding: utf-8 -*-
"""Untitled2.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1pQj4la1PcFuG2oZXIFSJyXsr6BVVuPWW
"""


import nest_asyncio
nest_asyncio.apply()

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.types import InputMediaPhoto
from aiogram import F as AF
from aiogram.types import ContentType


from aiogram.types.input_file import FSInputFile

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from PIL import Image

import torchvision.transforms as transforms
from torchvision.models import vgg19, VGG19_Weights

import copy

import os

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.set_default_device(device)

class ContentLoss(nn.Module):

    def __init__(self, target,):
        super(ContentLoss, self).__init__()
        # we 'detach' the target content from the tree used
        # to dynamically compute the gradient: this is a stated value,
        # not a variable. Otherwise the forward method of the criterion
        # will throw an error.
        self.target = target.detach()

    def forward(self, input):
        self.loss = F.mse_loss(input, self.target)
        return input

class StyleLoss(nn.Module):

    def __init__(self, target_feature):
        super(StyleLoss, self).__init__()
        self.target = gram_matrix(target_feature).detach()

    def forward(self, input):
        G = gram_matrix(input)
        self.loss = F.mse_loss(G, self.target)
        return input

class Normalization(nn.Module):
    def __init__(self, mean, std):
        super(Normalization, self).__init__()
        # .view the mean and std to make them [C x 1 x 1] so that they can
        # directly work with image Tensor of shape [B x C x H x W].
        # B is batch size. C is number of channels. H is height and W is width.
        self.mean = torch.tensor(mean).view(-1, 1, 1)
        self.std = torch.tensor(std).view(-1, 1, 1)

    def forward(self, img):
        # normalize ``img``
        return (img - self.mean) / self.std

def image_loader(image_name):
    imsize = 512 if torch.cuda.is_available() else 128  # use small size if no GPU

    loader = transforms.Compose([
        transforms.Resize(imsize),  # scale imported image
        transforms.ToTensor()])

    image = Image.open(image_name)
    # output type: PIL.JpegImagePlugin.JpegImageFile. Attributes: format ('JPEG'), size(650, 650), mode('RGB')
    # fake batch dimension required to fit network's input dimensions. Else torch.Size([3, 512, 512])
    image = loader(image).unsqueeze(0)
    # torch.Size([1, 3, 512, 512])
    return image.to(device, torch.float)

def unload_image(image):
    unloader = transforms.ToPILImage()
    im = unloader(image)
    return im

def gram_matrix(input):
    a, b, c, d = input.size()  # a=batch size(=1)
    # b=number of feature maps
    # (c,d)=dimensions of a f. map (N=c*d)

    features = input.view(a * b, c * d)  # resize F_XL into \hat F_XL

    G = torch.mm(features, features.t())  # compute the gram product

    # we 'normalize' the values of the gram matrix
    # by dividing by the number of element in each feature maps.
    return G.div(a * b * c * d)

def get_style_model_and_losses(cnn, normalization_mean, normalization_std,
                               style_img, content_img,
                               content_layers, style_layers):
    # normalization module
    normalization = Normalization(normalization_mean, normalization_std)

    # just in order to have an iterable access to or list of content/style
    # losses
    content_losses = []
    style_losses = []

    # assuming that ``cnn`` is a ``nn.Sequential``, so we make a new ``nn.Sequential``
    # to put in modules that are supposed to be activated sequentially
    model = nn.Sequential(normalization)

    i = 0  # increment every time we see a conv
    for layer in cnn.children():
        if isinstance(layer, nn.Conv2d):
            i += 1
            name = 'conv_{}'.format(i)
        elif isinstance(layer, nn.ReLU):
            name = 'relu_{}'.format(i)
            # The in-place version doesn't play very nicely with the ``ContentLoss``
            # and ``StyleLoss`` we insert below. So we replace with out-of-place
            # ones here.
            layer = nn.ReLU(inplace=False)
        elif isinstance(layer, nn.MaxPool2d):
            name = 'pool_{}'.format(i)
        elif isinstance(layer, nn.BatchNorm2d):
            name = 'bn_{}'.format(i)
        else:
            raise RuntimeError('Unrecognized layer: {}'.format(layer.__class__.__name__))

        model.add_module(name, layer)

        if name in content_layers:
            # add content loss:
            target = model(content_img).detach()
            content_loss = ContentLoss(target)
            model.add_module("content_loss_{}".format(i), content_loss)
            content_losses.append(content_loss)

        if name in style_layers:
            # add style loss:
            target_feature = model(style_img).detach()
            style_loss = StyleLoss(target_feature)
            model.add_module("style_loss_{}".format(i), style_loss)
            style_losses.append(style_loss)

    # now we trim off the layers after the last content and style losses
    for i in range(len(model) - 1, -1, -1):
        if isinstance(model[i], ContentLoss) or isinstance(model[i], StyleLoss):
            break

    model = model[:(i + 1)]

    return model, style_losses, content_losses

def create_network(style_img, content_img):
    cnn = vgg19(weights=VGG19_Weights.DEFAULT).features.eval()
    cnn_normalization_mean = torch.tensor([0.485, 0.456, 0.406])
    cnn_normalization_std = torch.tensor([0.229, 0.224, 0.225])
    content_layers_default = ['conv_4']
    style_layers_default = ['conv_1', 'conv_2', 'conv_3', 'conv_4', 'conv_5']
    model, style_losses, content_losses = get_style_model_and_losses(cnn, cnn_normalization_mean, cnn_normalization_std, style_img=style_img, content_img=content_img, content_layers=content_layers_default, style_layers=style_layers_default)
    return model, style_losses, content_losses

def run_style_transfer(model, style_losses, content_losses,
                       content_img, style_img, input_img, num_steps=300,
                       style_weight=1000000, content_weight=1):
    """Run the style transfer."""
    print('Building the style transfer model..')

    # We want to optimize the input and not the model parameters so we
    # update all the requires_grad fields accordingly
    input_img.requires_grad_(True)
    # We also put the model in evaluation mode, so that specific layers
    # such as dropout or batch normalization layers behave correctly.
    model.eval()
    model.requires_grad_(False)

    optimizer = optim.LBFGS([input_img])

    print('Optimizing..')
    run = [0]
    while run[0] <= num_steps:

        def closure():
            # correct the values of updated input image
            with torch.no_grad():
                input_img.clamp_(0, 1)

            optimizer.zero_grad()
            model(input_img)
            style_score = 0
            content_score = 0

            for sl in style_losses:
                style_score += sl.loss
            for cl in content_losses:
                content_score += cl.loss

            style_score *= style_weight
            content_score *= content_weight

            loss = style_score + content_score
            loss.backward()

            run[0] += 1
            if run[0] % 50 == 0:
                print("run {}:".format(run))
                print('Style Loss : {:4f} Content Loss: {:4f}'.format(
                    style_score.item(), content_score.item()))
                print()

            return style_score + content_score

        optimizer.step(closure)

    # a last correction...
    with torch.no_grad():
        input_img.clamp_(0, 1)

    return input_img

# Создаем объекты бота и диспетчера
bot = Bot(token=TG_BOT_TOKEN)
bot.set_my_description('Hi!This is a style transfer bot!\nFirst send me content picture, then style picture.\nPlease make sure you send them as photos, not as files.\nAfter you send both pictures, please run /create command')
dp = Dispatcher()

obj = {'content': False, 'style': False}

@dp.message(AF.photo)
async def get_photo(message: Message):
    if not obj['content']:
        obj['content'] = True
        await message.bot.download(file=message.photo[-1].file_id, destination='source_picture.jpg')
    elif not obj['style']:
        obj['style'] = True
        await message.bot.download(file=message.photo[-1].file_id, destination='style_picture.jpg')

@dp.message(Command(commands=["start"]))
async def process_start_command(message: Message):
    await message.answer('Hi!This is a style transfer bot!\nFirst send me content picture, then style picture.\nPlease make sure you send them as photos, not as files.\nAfter you send both pictures, please run /create command')


# Этот хэндлер будет срабатывать на команду "/help"
@dp.message(Command(commands=['help']))
async def process_help_command(message: Message):
    await message.answer(str(len(message.photo)))

@dp.message(Command(commands=['send']))
async def send(message: Message):
    photo = FSInputFile('final_picture.jpg')
    await message.answer_photo(photo, capture='123')

@dp.message(Command(commands=['create']))
async def get_photo(message: Message):
    await message.answer('Please wait while your picture is being prepared...')
    content_img = image_loader("source_picture.jpg")
    style_img = image_loader("style_picture.jpg")
    model, style_losses, content_losses = create_network(style_img, content_img)
    input_img = content_img.clone()
    final = run_style_transfer(model, style_losses, content_losses,
                       content_img, style_img, input_img)
    final_picture = unload_image(final.squeeze(0))
    final_picture.save('final_picture.jpg')
    photo = FSInputFile('final_picture.jpg')
    await message.answer_photo(photo, capture='Here is your picture!')

if __name__ == '__main__':
    dp.run_polling(bot)
