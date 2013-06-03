
"""
Contains a class for creating a plan, allocating arrays, compiling kernels and other things like that
"""
import time, math, os, logging
import gc
import numpy
import pyopencl, pyopencl.array
from .param import par
from .opencl import ocl
from .utils import calc_size
logger = logging.getLogger("sift.plan")

class SiftPlan(object):
    """
    How to calculate a set of SIFT keypoint on an image:

    siftp = sift.SiftPlan(img.shape,img.dtype,devicetype="GPU")
    kp = siftp.keypoints(img)

    kp is a nx132 array. the second dimension is composed of x,y, scale and angle as well as 128 floats describing the keypoint

    """
    def __init__(self, shape=None, dtype=None, devicetype="GPU", template=None, profile=False, device=None):
        """
        Contructor of the class
        """
        if template is not None:
            self.shape = template.shape
            self.dtype = template.dtype
        else:
            self.shape = shape
            self.dtype = numpy.dtype(dtype)
        if len(self.shape) == 3:
            self.RGB = True
            self.shape = self.shape[:2]
        elif len(self.shape) == 2:
            self.RGB = False
        else:
            raise RuntimeError("Unable to process image of shape %s" % (tuple(self.shape,)))
        self.profile = bool(profile)
        self.sigmaRatio = 2.0 ** (1.0 / par.Scales)
        self.scales = [] #in XY order
        self.procsize = []
        self.wgsize = []
        self.buffers = {}
        self.programs = {}
        self.memory = None
        self._calc_scales()
        self._calc_memory()
        if device is None:
            self.device = ocl.select_device(type=devicetype, memory=self.memory, best=True)
        else:
            self.device = device
        self.ctx = ctx = pyopencl.Context(devices=[pyopencl.get_platforms()[self.device[0]].get_devices()[self.device[1]]])
        print self.ctx.devices[0]
        if profile:
            self.queue = pyopencl.CommandQueue(self.ctx, properties=pyopencl.command_queue_properties.PROFILING_ENABLE)
        else:
            self.queue = pyopencl.CommandQueue(self.ctx)
        self._calc_workgroups()
        self._compile_kernels()
        self._allocate_buffers()

    def __del__(self):
        """
        Destructor: release all buffers
        """
        self._free_kernels()
        self._free_buffers()
        self.queue = None
        self.ctx = None
        gc.collect()

    def _calc_scales(self):
        """
        Nota scales are in XY order
        """
        self.scales = [tuple(numpy.int32(i) for i in self.shape[-1::-1])]
        shape = self.shape
        min_size = 2 * par.BorderDist + 2
        while min(shape) > min_size:
            shape = tuple(numpy.int32(i // 2) for i in shape)
            self.scales.append(shape)
        self.scales.pop()

    def _calc_memory(self):
        # Just the context + kernel takes about 75MB on the GPU
        self.memory = 75 * 2 ** 20
        size_of_float = numpy.dtype(numpy.float32).itemsize
        size_of_input = numpy.dtype(self.dtype).itemsize
        #raw images:
        size = self.shape[0] * self.shape[1]
        self.memory += size * (size_of_float + size_of_input) #raw_float + initial_image
        for scale in self.scales:
            nr_blur = par.Scales + 3
            nr_dogs = par.Scales + 2
            size = scale[0] * scale[1]
            self.memory += size * (nr_blur + nr_dogs + 1) * size_of_float  # 1 temporary array

        ########################################################################
        # Calculate space for gaussian kernels
        ########################################################################
        curSigma = 1.0 if par.DoubleImSize else 0.5
        if par.InitSigma > curSigma:
            sigma = math.sqrt(par.InitSigma ** 2 - curSigma ** 2)
            size = int(8 * sigma + 1)
            logger.debug("pre-Allocating %s float for init blur" % size)
            self.memory += size * size_of_float
        prevSigma = par.InitSigma
        for i in range(par.Scales + 2):
            increase = prevSigma * math.sqrt(self.sigmaRatio ** 2 - 1.0)
            size = int(8 * increase + 1)
            logger.debug("pre-Allocating %s float for blur sigma: %s" % (size, increase))
            self.memory += size * size_of_float
            prevSigma *= self.sigmaRatio;

    def _allocate_buffers(self):
        shape = self.shape
        if self.dtype != numpy.float32:
            self.buffers["raw"] = pyopencl.array.empty(self.queue, shape, dtype=self.dtype, order="C")
        self.buffers["input"] = pyopencl.array.empty(self.queue, shape, dtype=numpy.float32, order="C")
        for octave in range(len(self.scales)):
            self.buffers[(octave, "tmp") ] = pyopencl.array.empty(self.queue, shape, dtype=numpy.float32, order="C")
            for i in range(par.Scales + 3):
                self.buffers[(octave, i, "G") ] = pyopencl.array.empty(self.queue, shape, dtype=numpy.float32, order="C")
            for i in range(par.Scales + 2):
                self.buffers[(octave, i, "DoG") ] = pyopencl.array.empty(self.queue, shape, dtype=numpy.float32, order="C")
            shape = (shape[0] // 2, shape[1] // 2)

        ########################################################################
        # Allocate space for gaussian kernels
        ########################################################################
        curSigma = 1.0 if par.DoubleImSize else 0.5
        if par.InitSigma > curSigma:
            sigma = math.sqrt(par.InitSigma ** 2 - curSigma ** 2)
            self._init_gaussian(sigma)
        prevSigma = par.InitSigma

        for i in range(par.Scales + 2):
            increase = prevSigma * math.sqrt(self.sigmaRatio ** 2 - 1.0)
            self._init_gaussian(increase)
            prevSigma *= self.sigmaRatio


    def _init_gaussian(self, sigma):
        """
        Create a buffer of the right size according to the width of the gaussian ...
        
        @param  sigma: width of the gaussian, the length of the function will be 8*sigma + 1
        """
        name = "gaussian_%s" % sigma
        size = int(8 * sigma + 1)
        logger.debug("Allocating %s float for blur sigma: %s" % (size, sigma))
        self.buffers[name] = pyopencl.array.empty(self.queue, size, dtype=numpy.float32, order="C")
#            Norming the gaussian would takes three OCL kernel launch (gaussian, calc_sum and norm) -> calculation done on CPU
        x = numpy.arange(size) - (size - 1.0) / 2.0
        gaussian = numpy.exp(-(x / sigma) ** 2 / 2.0).astype(numpy.float32)
        gaussian /= gaussian.sum(dtype=numpy.float32)
        self.buffers[name].set(gaussian)


    def _free_buffers(self):
        """
        free all memory allocated on the device
        """
        for buffer_name in self.buffers:
            if self.buffers[buffer_name] is not None:
                try:
                    del self.buffers[buffer_name]
                    self.buffers[buffer_name] = None
                except pyopencl.LogicError:
                    logger.error("Error while freeing buffer %s" % buffer_name)

    def _compile_kernels(self):
        """
        Call the OpenCL compiler
        """
        kernels = ["convolution", "preprocess", "algebra"]
        for kernel in kernels:
            kernel_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), kernel + ".cl")
            kernel_src = open(kernel_file).read()
            try:
                program = pyopencl.Program(self.ctx, kernel_src).build()
            except pyopencl.MemoryError as error:
                raise MemoryError(error)
            self.programs[kernel] = program

    def _free_kernels(self):
        """
        free all kernels
        """
        self.programs = {}

    def _calc_workgroups(self):
        """
        First try to guess the best workgroup size, then calculate all global worksize
        
        Nota: 
        The workgroup size is limited by the device  
        The workgroup size is limited to the 2**n below then image size (hence changes with octaves)
        The second dimension of the wg size should be large, the first small: i.e. (1,64)
        The processing size should be a multiple of  workgroup size.
        """
        device = self.ctx.devices[0]
        max_work_group_size = device.max_work_group_size
        max_work_item_sizes = device.max_work_item_sizes
        #we recalculate the shapes ...
        shape = self.shape
        min_size = 2 * par.BorderDist + 2
        while min(shape) > min_size:
            wg = (1, min(2 ** int(math.log(shape[1]) / math.log(2)), max_work_item_sizes[1]))
            self.wgsize.append(wg)
            self.procsize.append(calc_size(shape, wg))
            shape = tuple(i // 2 for i in shape)




    def keypoints(self, image):
        """
        Calculates the keypoints of the image
        @param image: ndimage of 2D (or 3D if RGB)  
        """
        assert image.shape == self.shape
        assert image.dtype == self.dtype
        t0 = time.time()
        if self.dtype == numpy.float32:
            pyopencl.enqueue_copy(self.queue, self.buffers["input"].data, image)
        elif self.dtype == numpy.uint8:
            pyopencl.enqueue_copy(self.queue, self.buffers["raw"].data, image)
            if self.RGB:
                self.programs["preprocess"].rgb_to_float(self.queue, self.procsize[0], self.wgsize[0],
                                                         self.buffers["raw"].data, self.buffers["input"].data, *self.scales[0])
            else:
                self.programs["preprocess"].u8_to_float(self.queue, self.procsize[0], self.wgsize[0],
                                                         self.buffers["raw"].data, self.buffers["input"].data, *self.scales[0])
        elif self.dtype == numpy.uint16:
            pyopencl.enqueue_copy(self.queue, self.buffers["raw"].data, image)
            self.programs["preprocess"].u16_to_float(self.queue, self.procsize[0], self.wgsize[0],
                                                         self.buffers["raw"].data, self.buffers["input"].data, *self.scales[0])
        elif self.dtype == numpy.int32:
            pyopencl.enqueue_copy(self.queue, self.buffers["raw"].data, image)
            self.programs["preprocess"].s32_to_float(self.queue, self.procsize[0], self.wgsize[0],
                                                         self.buffers["raw"].data, self.buffers["input"].data, *self.scales[0])
        elif self.dtype == numpy.int64:
            pyopencl.enqueue_copy(self.queue, self.buffers["raw"].data, image)
            self.programs["preprocess"].s64_to_float(self.queue, self.procsize[0], self.wgsize[0],
                                                         self.buffers["raw"].data, self.buffers["input"].data, *self.scales[0])
        else:
            raise RuntimeError("invalid input format error")
        min_data = pyopencl.array.min(self.buffers["input"], self.queue).get()
        max_data = pyopencl.array.max(self.buffers["input"], self.queue).get()
        self.programs["preprocess"].normalizes(self.queue, self.procsize[0], self.wgsize[0],
                                               self.buffers["input"].data,
                                               numpy.float32(min_data), numpy.float32(max_data), numpy.float32(255.), *self.scales[0])


        octSize = 1.0
        curSigma = 1.0 if par.DoubleImSize else 0.5
        octave = 0
        if par.InitSigma > curSigma:
            logger.debug("Bluring image to achieve std: %f", par.InitSigma)
            sigma = math.sqrt(par.InitSigma ** 2 - curSigma ** 2)
            self.gaussian_convolution(self.buffers["input"], self.buffers[(0, 0, "G")], sigma, 0)
        else:
            pyopencl.enqueue_copy(self.queue, dest=self.buffers[(0, 0, "G")].data, src=self.buffers["input"].data)
        ########################################################################
        # Calculate gaussian blur and DoG for every octave
        ########################################################################
        for octave in range(len(self.scales)):
            prevSigma = par.InitSigma
            logger.debug("Working on octave %i" % octave)
            for i in range(par.Scales + 2):
                sigma = prevSigma * math.sqrt(self.sigmaRatio ** 2 - 1.0)
                logger.debug("blur with sigma %s" % sigma)
                self.gaussian_convolution(self.buffers[(octave, i, "G")], self.buffers[(octave, i + 1, "G")], sigma, octave)
                prevSigma *= self.sigmaRatio
                self.programs["algebra"].combine(self.queue, self.procsize[octave], self.wgsize[octave],
                                                 self.buffers[(octave, i + 1, "G")].data, numpy.float32(1.0),
                                                 self.buffers[(octave, i    , "G")].data, numpy.float32(-1.0),
                                                 self.buffers[(octave, i, "DoG")].data, *self.scales[octave])
#                self.buffers[(octave, i, "DoG")]. = self.buffers[(octave, i + 1, "G")] - self.buffers[(octave, i, "G")]
            if i < par.Scales + 1:
                k1 = self.programs["preprocess"].shrink(self.queue, self.procsize[octave + 1], self.wgsize[octave + 1],
                                                    self.buffers[(octave, 0, "G")].data, self.buffers[(octave + 1, 0, "G")].data,
                                                    numpy.int32(2), numpy.int32(2), *self.scales[octave + 1])

        print("Execution time: %.3fs" % (time.time() - t0))
    def gaussian_convolution(self, input_data, output_data, sigma, octave=0):
        """
        Calculate the gaussian convolution with precalculated kernels.
        
        Uses a temporary buffer
        """
        temp_data = self.buffers[(octave, "tmp") ]
        gaussian = self.buffers["gaussian_%s" % sigma]
        k1 = self.programs["convolution"].horizontal_convolution(self.queue, self.procsize[octave], self.wgsize[octave],
                                input_data.data, temp_data.data, gaussian.data, numpy.int32(gaussian.size), *self.scales[octave])
        k2 = self.programs["convolution"].vertical_convolution(self.queue, self.procsize[octave], self.wgsize[octave],
                                temp_data.data, output_data.data, gaussian.data, numpy.int32(gaussian.size), *self.scales[octave])
        if self.profile:
            k2.wait()
            logger.info("Blur sigma %s octave %s took %.3fms + %.3fms" % (sigma, octave, 1e-6 * (k1.profile.end - k1.profile.start),
                                                                                          1e-6 * (k2.profile.end - k2.profile.start)))

if __name__ == "__main__":
    #Prepare debugging
    import scipy.misc
    lena = scipy.lena()
    s = SiftPlan(template=lena)
    s.keypoints(lena)

